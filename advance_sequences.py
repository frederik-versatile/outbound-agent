"""Third cron entrypoint (separate from orchestrator.py and poll_for_edits.py):
advances leads through their outreach sequence — see config/sequences/<id>.yaml
and sequencing.py's module docstring for the state machine.

    python advance_sequences.py --deployment acme-corp [--dry-run]

For every lead whose reply-check timer has elapsed:
  - checks the thread for a reply (mailbox.check_reply) — if found, marks
    the lead "replied" and stops; nothing further happens to that lead.
  - if not, and there's a next step in the sequence, drafts it (in the SAME
    thread, referencing the prior step's actual text) via sequence_agent.
  - if that was the last step, starts the recycle clock instead.

Also flips any lead whose recycle clock has elapsed back to "eligible", so
orchestrator.py's suppression check stops excluding them.

--dry-run prints what it would do without calling sequence_agent or writing
any sequence state — the reply/recycle CHECKS still happen (read-only
mailbox calls), only the resulting state transitions are skipped.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, create_sdk_mcp_server, query

import sequencing
from billing import SubscriptionInactive, require_active_subscription
from deployment import Deployment, load_deployment
from clients.mailbox_client import MailboxClient
from contacts import ContactVault
from pii import scrub_text
from tools.fs_tools import build_sequence_fs_tools
from tools.mailbox_tools import mailbox_server
from agents import sequence_agent


def _find_audit_entry(deployment: Deployment, tracking_id: str) -> dict[str, Any] | None:
    raw = deployment.store.read_text(deployment.state_key("drafts_audit.json"))
    for entry in (json.loads(raw) if raw else []):
        if entry["tracking_id"] == tracking_id:
            return entry
    return None


async def _draft_next_step(
    deployment: Deployment, entry: dict[str, Any], next_step: int, in_reply_to: str | None
) -> dict[str, Any] | None:
    prior_audit_entry = _find_audit_entry(deployment, entry["step_tracking_id"])
    vault = ContactVault(deployment)
    contact = vault.get(entry.get("person_id")) or {"email": entry["lead_key"], "name": entry.get("stakeholder_name", "")}
    # before_text is stored with placeholders; depersonalize + scrub covers older entries that weren't.
    prior_text = (scrub_text(ContactVault.depersonalize(prior_audit_entry["before_text"], contact,
                                                        getattr(deployment, 'sender_names', [])))
                  if prior_audit_entry else "(prior email text unavailable)")
    step = deployment.sequence.steps[next_step]

    run_dir = deployment.run_dir(f"advance-{uuid.uuid4()}")
    mailbox = MailboxClient(deployment)
    mcp_mailbox = mailbox_server(
        mailbox, deployment, max_drafts_per_run=1, run_dir=run_dir,
        thread_id=entry["thread_id"], in_reply_to=in_reply_to, sequence_step=next_step,
        fixed_contact=contact,
    )

    options = ClaudeAgentOptions(
        tools=[],
        system_prompt=sequence_agent.SYSTEM_PROMPT,
        allowed_tools=sequence_agent.ALLOWED_TOOLS,
        mcp_servers={
            "fs": create_sdk_mcp_server(name="fs", tools=build_sequence_fs_tools(deployment)),
            "mailbox": mcp_mailbox,
        },
        permission_mode="bypassPermissions",
        model=deployment.models.get("drafting"),
        max_turns=20,
    )
    prompt = (
        f"Account: {entry['account_name']}\n"
        f"Stakeholder: {entry.get('title') or contact.get('title') or 'a contact'} (recipient fixed by the code)\n"
        f"Original subject: {scrub_text(ContactVault.depersonalize(entry.get('subject') or '(unknown)', contact, getattr(deployment, 'sender_names', [])))}\n\n"
        f"PRIOR EMAIL in this sequence (step {entry['step']}, do not repeat verbatim):\n"
        f"{prior_text}\n\n"
        f"THIS STEP's angle ({step.name}):\n{step.angle}\n\n"
        f"Draft this step now."
    )
    async for message in query(prompt=prompt, options=options):
        print(f"[advance_sequences] {str(message)[:500]}")

    drafts_created_path = run_dir / "drafts_created.json"
    if not drafts_created_path.exists():
        return None
    created = json.loads(drafts_created_path.read_text())
    return created[0] if created else None


async def run_once(deployment: Deployment, dry_run: bool) -> dict[str, int]:
    stats = {"replied": 0, "advanced": 0, "recycling_started": 0, "recycled_eligible": 0, "no_draft_produced": 0}

    due = sequencing.due_for_reply_check(deployment)
    # Built lazily: a fresh deployment with no leads yet due for a reply
    # check shouldn't need working mailbox OAuth just to run this cron job.
    mailbox = MailboxClient(deployment) if due else None

    for entry in due:
        result = mailbox.check_reply(entry["thread_id"], entry["lead_key"])

        if result["has_reply"]:
            print(f"[advance_sequences] {entry.get('title') or entry.get('stakeholder_title') or 'contact'} ({entry['account_name']}): replied. Stopping.")
            if not dry_run:
                sequencing.mark_replied(deployment, entry["lead_key"])
            stats["replied"] += 1
            continue

        next_step = entry["step"] + 1
        if next_step >= len(deployment.sequence.steps):
            print(f"[advance_sequences] {entry.get('title') or entry.get('stakeholder_title') or 'contact'} ({entry['account_name']}): "
                  f"no reply, sequence exhausted. Starting {deployment.sequence.recycle_after_days}-day recycle clock.")
            if not dry_run:
                sequencing.start_recycle_clock(deployment, entry["lead_key"])
            stats["recycling_started"] += 1
            continue

        print(f"[advance_sequences] {entry.get('title') or entry.get('stakeholder_title') or 'contact'} ({entry['account_name']}): "
              f"no reply, advancing to step {next_step} ({deployment.sequence.steps[next_step].name}).")
        if dry_run:
            stats["advanced"] += 1
            continue

        draft = await _draft_next_step(deployment, entry, next_step, result["last_message_id_header"])
        if draft is None:
            print(f"[advance_sequences] WARNING: no draft produced for {entry['lead_key']}; will retry next run.")
            stats["no_draft_produced"] += 1
            continue

        sequencing.advance_to_next_step(
            deployment, entry["lead_key"], next_step, draft["tracking_id"], draft["thread_id"], draft.get("subject")
        )
        stats["advanced"] += 1

    for entry in sequencing.due_for_recycle(deployment):
        print(f"[advance_sequences] {entry.get('title') or entry.get('stakeholder_title') or 'contact'} ({entry['account_name']}): "
              f"recycle window elapsed, now eligible for fresh outreach.")
        if not dry_run:
            sequencing.mark_eligible(deployment, entry["lead_key"])
        stats["recycled_eligible"] += 1

    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Advance leads through their outreach sequence")
    parser.add_argument("--deployment", required=True)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Check for replies/recycle eligibility and print what would happen, but don't draft "
             "follow-ups or write any sequence state.",
    )
    parser.add_argument(
        "--skip-billing-check", action="store_true",
        help="Skip the Stripe subscription check. Local dev/testing only.",
    )
    args = parser.parse_args()

    deployment = load_deployment(args.deployment)

    if not args.skip_billing_check:
        try:
            require_active_subscription(deployment)
        except SubscriptionInactive as exc:
            print(f"[advance_sequences] BILLING GATE: {exc}")
            raise SystemExit(1)

    stats = asyncio.run(run_once(deployment, args.dry_run))
    print(f"[advance_sequences] done. {stats}")


if __name__ == "__main__":
    main()
