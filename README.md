# Outbound Agent

Turns a customer's ICP criteria doc into a multi-step outreach sequence,
**drafted** (never sent) directly in their Gmail or Outlook mailbox, that
branches on whether the lead replies, and learns from how a human edits
drafts so the next batch is sharper.

Pipeline: **prioritize accounts → find stakeholders + verified emails →
draft the cold opener → (async, days later) check for a reply → draft the
next step or start a recycle cooldown → (async, separately) learn from
edits.** Each stage is its own Claude Agent SDK call with its own system
prompt, tool allowlist, and model — see `orchestrator.py`'s module
docstring for why stages aren't delegated to autonomously by one top-level
agent.

### The sequence

```
config/sequences/<id>.yaml → opener (step 0) → wait N days → replied? ──yes──▶ done
                                                    │no
                                                    ▼
                                              bump (step 1) → wait N days → replied? ──yes──▶ done
                                                    │no
                                                    ▼
                                          breakup (step 2, last) → wait N days → replied? ──yes──▶ done
                                                    │no
                                                    ▼
                                    recycle cooldown (config: recycle_after_days) → eligible again
```

Every step after the opener lands in the **same email thread** as the step
before it (`thread_id`/`in_reply_to`), not a fresh cold email. Branching is
reply-only for v1 — see "Engagement tracking" below for why open/click
tracking isn't implemented. `sequencing.py` owns the state machine
(`state/<id>/sequences.json`, one entry per lead) and is what stops the
pipeline from drafting a fresh opener to the same person every single day:
`orchestrator.py` suppresses anyone already mid-sequence, already replied,
or still inside the recycle cooldown before drafting.

#### Engagement tracking

Gmail and Outlook expose no API for "did they open/click this" — there's no
webhook for it. HubSpot doesn't help either, even with the mailbox
connected to HubSpot's Sales extension: HubSpot's own community forum,
knowledge base, and the documented properties on the CRM `emails` object
all confirm connected-inbox open/click data is UI-only, not exposed via
their API (checked directly, not assumed — see the API's actual property
list: `hs_email_status`, `hs_email_subject`, from/to fields, no tracking
metrics). The only way to get real open/click data at all is a self-hosted
tracking pixel + rewritten links, which needs an always-on web service (not
just cron jobs) and is a genuinely noisy signal in practice (corporate
security scanners auto-open/click inbound mail while scanning for threats,
before a human ever sees it). v1 deliberately skips this and branches on
reply only — `advance_sequences.py` is where that would plug in if it's
ever added.

### Hire-sheet cross-reference

Some customers already track their own buying signal in a spreadsheet
(e.g. "companies currently hiring a videographer"). `config/deployments/
<id>.yaml`'s `hire_sheet:` section (disabled by default) points at that
sheet; `orchestrator.py`'s `cross_reference_hire_sheet()` reads it via a
read-only Google Sheets service account (`clients/sheets_client.py`,
`setup_google_sheets.py`) and **boosts, never introduces** — Apollo search
from the ICP doc is still the only way an account enters the pipeline; a
domain match on the sheet just raises that account's score (capped at 100)
and appends a rationale note. A sheet read failure logs a warning and
leaves scores untouched rather than failing the whole run over what's
meant to be a secondary signal.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt   # add -dev for pytest; requirements.txt alone is enough to run
cp .env.example .env                  # fill in ANTHROPIC_API_KEY
```

### Onboard a customer deployment

Every customer is a client project in the Agents workspace, and its outbound config lives in that
client's folder (see `deployment.py`):

```bash
C="../../../Clients/Acme Corp/outbound" && mkdir -p "$C"
cp config/deployments/_example.yaml "$C/outbound.yaml"
cp config/icp_criteria/_example.md   "$C/icp.md"
cp config/positioning/_example.md    "$C/positioning.md"
cp config/sequences/_example.yaml    "$C/sequence.yaml"
```

Edit `outbound.yaml`: set `deployment_id: acme-corp`, `mail_provider`, safety limits and
`sender_names`. Fill in the ICP and positioning docs with the customer's real criteria/pitch, and
tune the sequence doc's step wait-times/angles/recycle window if the defaults (4 days per step,
90-day recycle) aren't right for this customer. Register it on the client (lock register):

```bash
cd "../../Client template" && python3 new_client.py "Acme Corp" --outbound acme-corp --refresh
```

Store the client's Apollo key in the macOS Keychain (hidden prompt, validated first):

```bash
python setup_apollo.py --deployment acme-corp
```

Optional — only if this customer has a hire-signal sheet to cross-reference
(see "Hire-sheet cross-reference" above): set `hire_sheet.enabled: true` and
`spreadsheet_id`/`range` in `acme-corp.yaml`, share the sheet with the
service account's email, then:

```bash
python setup_google_sheets.py --deployment acme-corp --key-file ~/Downloads/service-account-key.json
```

### Mailbox OAuth

OAuth tokens live in `deployment.store` (see `store.py`), not local files —
plain disk locally (`REDIS_URL` unset), Render's Key Value store in
production. Run these setup scripts with `REDIS_URL` pointed at the same
instance the cron jobs use (see "Cloud deployment" below) so the token
lands where production will look for it.

**Gmail**: download an OAuth client ID (Desktop app type) from Google Cloud
Console for a project with the Gmail API enabled, then:

```bash
python setup_oauth_gmail.py --deployment acme-corp \
    --client-secret-file ~/Downloads/client_secret_1234.json
```

**Outlook**: register a public-client app in Azure AD / Entra ID with the
`Mail.ReadWrite` delegated permission, put its `client_id`/`tenant_id` in
`acme-corp.yaml` under `outlook:`, then:

```bash
python setup_oauth_outlook.py --deployment acme-corp
```

## Running it

```bash
# Offline, zero-cost: replays fixtures, no live Apollo/mailbox calls at all
python orchestrator.py run --deployment acme-corp --dry-run --use-fixtures

# Live Apollo + mailbox drafting, but every deployment starts as dry-run
# (safety.dry_run_default: true) until you flip that in the YAML or pass --live
python orchestrator.py run --deployment acme-corp --dry-run

# Resume a crashed run from a later stage
python orchestrator.py run --deployment acme-corp --resume-from discovery --run-id 20260922-101500
```

Before pointing this at a real customer mailbox, stage it against a
throwaway inbox you own: create a separate `test.yaml` deployment (own
secrets, own Apollo key or `--use-fixtures`) and run against `--deployment
test --live` first.

### The learning loop (run separately, e.g. via cron)

```bash
python poll_for_edits.py --deployment acme-corp --dry-run   # see what it would detect
python poll_for_edits.py --deployment acme-corp             # detect + update style_notes.md
```

`state/<deployment_id>/learning/style_notes.md` is human-readable — a
customer's sales ops person can open and hand-edit it directly, and the
drafting stage will pick that up on its next run.

Also flips a sequence lead's entry from `drafted` to `awaiting_reply` once
it confirms a step was actually sent (see `sequencing.mark_sent`) — this is
what starts that step's reply-check timer, so `advance_sequences.py` (below)
depends on this having run first.

### Advancing sequences (run separately, e.g. via cron)

```bash
python advance_sequences.py --deployment acme-corp --dry-run   # see what it would detect/do
python advance_sequences.py --deployment acme-corp             # check replies, draft next steps, recycle
```

For every lead whose reply-check timer has elapsed: checks the thread for a
reply and stops if found; otherwise drafts the next step in the same
thread, or starts the recycle cooldown if that was the last step. Also
flips any lead whose recycle cooldown has elapsed back to `eligible`.

### Dashboard

```bash
DASHBOARD_USERNAME=admin DASHBOARD_PASSWORD=yourpassword python dashboard.py --deployment acme-corp
```

Then visit `http://127.0.0.1:5000` (basic-auth prompt, use the credentials
above). Shows sends, replies, reply rate, and who was emailed — computed
from `drafts_audit.json` and `sequences.json`, the same data every other
part of this project already writes; the dashboard is a read-only view, not
a new data source. "Sends" only counts drafts `poll_for_edits.py` has
confirmed were actually sent — a draft still sitting untouched in Gmail/
Outlook doesn't count, by design (see `dashboard.py`'s `SENT_STATUSES`).
Fails closed on auth: if `DASHBOARD_USERNAME`/`DASHBOARD_PASSWORD` aren't
set, every request gets a 401, never an open page.

## Cloud deployment (Render)

> Client configs now live outside this repo (`Agents/Clients/<Client>/outbound/`). Before a cloud
> deploy, ship the client's `outbound/` folder with the service and point `OUTBOUND_CLIENTS_DIR`
> at its parent; the Apollo key comes from the env var named by `apollo_api_key_env` there (no
> Keychain in the cloud).

This is meant to run unattended: the client never touches it, they only see
drafts appear in their own inbox, and access is gated by whether their
subscription is active. `render.yaml` defines the whole thing as one
Blueprint — a daily cron job for the pipeline, a cron job every 4 hours for
the learning loop, a cron job every 4 hours (offset) for advancing
sequences, a free web service for the dashboard, and a Render Key Value
(Redis-compatible) instance that holds everything that has to survive
between runs (OAuth tokens, the drafts audit trail, `style_notes.md`,
`sequences.json`). Render Cron Jobs reset their filesystem on every run —
that's why this state can't just live on local disk in production, and why
`store.py` exists.

**Cost**: Render itself is paid once this is live — roughly $10/mo for the
Key Value instance (the smallest *persistent* tier; Render's free Key Value
tier explicitly isn't durable across restarts, so it's not used here) plus
each of the three cron jobs' $1/mo minimum, prorated up by actual runtime.
The dashboard runs on Render's free Web Service tier (confirmed it can
reach the Key Value store over Render's private network even on free —
free services can send private-network requests, just not receive them),
so it adds no cost beyond the ~1 minute cold start after 15 minutes idle.
Verify current numbers in the Render dashboard before going live. This is
separate from, and on top of, whatever the client's own Anthropic/Apollo
usage costs.

1. Push this repo to your own private GitHub repo.
2. In `render.yaml`, replace every `acme-corp` with your real
   `deployment_id` (including `DEPLOYMENT_ID` on the dashboard service),
   and `APOLLO_API_KEY_ACME_CORP` with your real env var name (matching
   `apollo_api_key_env` in that deployment's YAML).
3. In the Render dashboard: **New → Blueprint**, connect the repo. Render
   reads `render.yaml` and creates all five services.
4. Set the `sync: false` env vars in the dashboard (never commit these):
   `ANTHROPIC_API_KEY`, your `APOLLO_API_KEY_...` var, `STRIPE_API_KEY`,
   `DASHBOARD_USERNAME`, `DASHBOARD_PASSWORD`.
5. Run the one-time OAuth setup scripts **locally**, with `REDIS_URL` set to
   the connection string Render shows for the `outbound-agent-kv` service
   (Render → that service → Connect), so the tokens land in the same store
   the cron jobs read from:
   ```bash
   REDIS_URL="<from Render dashboard>" python setup_oauth_gmail.py --deployment acme-corp --client-secret-file ...
   ```
6. Set `billing.stripe_subscription_id` in `config/deployments/acme-corp.yaml`
   to the subscription id Stripe gives you for that customer's Product/Price,
   then commit and push — all three cron jobs refuse to run without it.
7. Flip `safety.dry_run_default: false` in that deployment's YAML once
   you're ready for real drafts instead of previews, commit, push.

**Billing**: `billing.py`'s `require_active_subscription()` runs as the very
first thing all three entrypoints (`orchestrator.py`, `poll_for_edits.py`,
`advance_sequences.py`) do — it looks up the subscription via
`STRIPE_API_KEY` and exits before any Apollo or mailbox call if the status
isn't `active`/`trialing`. So payment lapsing just means the cron jobs log
a refusal and do nothing — no separate mechanism needed to "turn off" a
non-paying customer.

## Safety

**No PII reaches Claude.** Apollo people results go into a code-only contact store
(`contacts.py`); every agent works with `person_id` + title, seniority and company. Drafts are
written with `{{first_name}}`-style placeholders that the code fills in when it creates the draft;
the audit trail keeps the placeholder version, and edits are depersonalized (`{{first_name}}`,
`{{email}}`, `{{sender_name}}`) before the learning stage sees them. `pii.scrub` is a backstop on
every tool result. Tested in `tests/test_no_pii.py`.

This codebase can create email drafts. **It cannot send email — the
capability does not exist anywhere in the code**, not merely "the model is
told not to":

- `clients/gmail_client.py` and `clients/outlook_client.py` define
  `create_draft`/`get_draft`/`list_drafts` and nothing that sends.
- `tests/test_no_send_capability.py` statically scans `clients/` and
  `tools/` on every test run and fails if any method or tool name matching
  `send`/`sendMail` ever appears.
- Every stage's `ClaudeAgentOptions` sets `tools=[]`, disabling **all**
  built-in tools (Bash, Read, Write, WebFetch, ...) — each stage can only
  call the specific MCP tools it's been given. `permission_mode` is set to
  `bypassPermissions` only because the tool surface is already this narrow;
  it does not widen it.
- Apollo/Gmail/Outlook hosts are hardcoded in each client, not configurable
  by tool input.
- `max_accounts_per_run`, `max_contacts_per_account`,
  `max_total_enrich_calls_per_run`, and `max_drafts_per_run` are each
  enforced twice: once by `orchestrator.py` truncating a stage's output
  before the next stage's prompt is built, and again inside the tool
  wrapper the model actually calls.
- Every deployment starts in dry-run mode (`safety.dry_run_default: true`)
  until a human explicitly flips it.
- `learning_agent` (the stage that fires automatically, unattended, on a
  schedule) has zero mailbox-write tools registered — it is structurally
  unable to touch any draft, not just instructed not to.
- `sequencing.is_suppressed()` is a hard Python-level filter, not agent
  judgment — the model never decides whether someone's already been
  contacted; `orchestrator.py` removes them from the list before the
  drafting stage's prompt is even built.
- The paywall is enforced the same way: `require_active_subscription()`
  runs before any paid API call, in code, in all three entrypoints — not
  billed separately from whether the agent is actually running.

## Layout

```
deployment.py             Resolves --deployment <id> into config/store/env
store.py                  Durable key/value storage: local files or Render Key Value
sequencing.py             Per-lead sequence state machine (state/<id>/sequences.json)
billing.py                Stripe subscription gate, called first by all three entrypoints
orchestrator.py            Live pipeline: prioritize -> discover -> draft (step 0 only)
poll_for_edits.py          Async learning loop + confirms sends for sequencing (cron)
advance_sequences.py       Reply detection, follow-up/breakup drafting, recycling (cron)
dashboard.py               Read-only sends/replies/reply-rate view (Flask, basic-auth, web service)
setup_oauth_gmail.py        One-time Gmail OAuth per deployment
setup_oauth_outlook.py      One-time Outlook OAuth per deployment
setup_apollo.py             Validates an Apollo API key per deployment
setup_google_sheets.py      One-time Sheets service-account setup (optional, hire-sheet only)
render.yaml                Cloud deployment blueprint (3 cron jobs + dashboard web service + Key Value)
config/                     _example templates only; real configs live in Clients/<Client>/outbound/
                            (outbound.yaml, icp.md, positioning.md, sequence.yaml)
contacts.py                 Code-only contact store + placeholder merge/depersonalize (no-PII rule)
pii.py                      Output scrub backstop (mirrors Agents/Global/Shared/pii_guard.py)
secrets/                    Local-dev-only OAuth token cache (gitignored) — see store.py
clients/                    Thin API wrappers (Apollo, Gmail, Outlook, Sheets, mailbox facade)
agents/                     System prompt + tool allowlist per pipeline stage
tools/                      @tool wrappers exposing clients to each stage
state/<id>/runs/            Per-run scratch files only (ephemeral, local disk is fine)
tests/                      Unit tests + offline fixtures
```
