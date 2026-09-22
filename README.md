# Outbound Agent

Turns a customer's ICP criteria doc into personalized outreach emails,
**drafted** (never sent) directly in their Gmail or Outlook mailbox, then
learns from how a human edits those drafts so the next batch is sharper.

Pipeline: **prioritize accounts → find stakeholders + verified emails →
draft emails → (async, later) learn from edits.** Each stage is its own
Claude Agent SDK call with its own system prompt, tool allowlist, and model —
see `orchestrator.py`'s module docstring for why stages aren't delegated to
autonomously by one top-level agent.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt   # add -dev for pytest; requirements.txt alone is enough to run
cp .env.example .env                  # fill in ANTHROPIC_API_KEY
```

### Onboard a customer deployment

```bash
cp config/deployments/_example.yaml config/deployments/acme-corp.yaml
cp config/icp_criteria/_example.md   config/icp_criteria/acme-corp.md
cp config/positioning/_example.md    config/positioning/acme-corp.md
```

Edit `acme-corp.yaml`: set `deployment_id: acme-corp` (must match the
filename), `mail_provider`, safety limits, and `apollo_api_key_env`. Fill in
the ICP and positioning docs with the customer's real criteria/pitch.

Add the Apollo key to `.env`:

```
APOLLO_API_KEY_ACME_CORP=...
```

(the name just has to match what `apollo_api_key_env` in the deployment
YAML says).

```bash
python setup_apollo.py --deployment acme-corp
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

## Cloud deployment (Render)

This is meant to run unattended: the client never touches it, they only see
drafts appear in their own inbox, and access is gated by whether their
subscription is active. `render.yaml` defines the whole thing as one
Blueprint — a daily cron job for the pipeline, a cron job every 4 hours for
the learning loop, and a Render Key Value (Redis-compatible) instance that
holds everything that has to survive between runs (OAuth tokens, the drafts
audit trail, `style_notes.md`). Render Cron Jobs reset their filesystem on
every run — that's why this state can't just live on local disk in
production, and why `store.py` exists.

1. Push this repo to your own private GitHub repo.
2. In `render.yaml`, replace every `acme-corp` with your real
   `deployment_id`, and `APOLLO_API_KEY_ACME_CORP` with your real env var
   name (matching `apollo_api_key_env` in that deployment's YAML).
3. In the Render dashboard: **New → Blueprint**, connect the repo. Render
   reads `render.yaml` and creates all three services.
4. Set the `sync: false` env vars in the dashboard (never commit these):
   `ANTHROPIC_API_KEY`, your `APOLLO_API_KEY_...` var, `STRIPE_API_KEY`.
5. Run the one-time OAuth setup scripts **locally**, with `REDIS_URL` set to
   the connection string Render shows for the `outbound-agent-kv` service
   (Render → that service → Connect), so the tokens land in the same store
   the cron jobs read from:
   ```bash
   REDIS_URL="<from Render dashboard>" python setup_oauth_gmail.py --deployment acme-corp --client-secret-file ...
   ```
6. Set `billing.stripe_subscription_id` in `config/deployments/acme-corp.yaml`
   to the subscription id Stripe gives you for that customer's Product/Price,
   then commit and push — both cron jobs refuse to run without it.
7. Flip `safety.dry_run_default: false` in that deployment's YAML once
   you're ready for real drafts instead of previews, commit, push.

**Billing**: `billing.py`'s `require_active_subscription()` runs as the very
first thing both `orchestrator.py` and `poll_for_edits.py` do — it looks up
the subscription via `STRIPE_API_KEY` and exits before any Apollo or mailbox
call if the status isn't `active`/`trialing`. So payment lapsing just means
the cron jobs log a refusal and do nothing — no separate mechanism needed to
"turn off" a non-paying customer.

## Safety

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
- The paywall is enforced the same way: `require_active_subscription()`
  runs before any paid API call, in code, in both entrypoints — not billed
  separately from whether the agent is actually running.

## Layout

```
deployment.py             Resolves --deployment <id> into config/store/env
store.py                  Durable key/value storage: local files or Render Key Value
billing.py                Stripe subscription gate, called first by both entrypoints
orchestrator.py            Live pipeline: prioritize -> discover -> draft
poll_for_edits.py          Async learning loop (run separately/on a schedule)
setup_oauth_gmail.py        One-time Gmail OAuth per deployment
setup_oauth_outlook.py      One-time Outlook OAuth per deployment
setup_apollo.py             Validates an Apollo API key per deployment
render.yaml                Cloud deployment blueprint (2 cron jobs + Key Value)
config/deployments/         One YAML per customer (mail provider, limits, billing id)
config/icp_criteria/        One ICP doc per customer
config/positioning/         One positioning/value-prop doc per customer
secrets/                    Local-dev-only OAuth token cache (gitignored) — see store.py
clients/                    Thin API wrappers (Apollo, Gmail, Outlook, facade)
agents/                     System prompt + tool allowlist per pipeline stage
tools/                      @tool wrappers exposing clients to each stage
state/<id>/runs/            Per-run scratch files only (ephemeral, local disk is fine)
tests/                      Unit tests + offline fixtures
```
