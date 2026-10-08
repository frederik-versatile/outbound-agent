"""Separate entrypoint (run on a cron/launchd schedule, not by orchestrator.py)
for the async learning loop: notices drafts that have been sent or (optionally)
settled into a stable edited state, diffs them against what the agent
originally generated, and invokes learning_agent to update style_notes.md.

    python poll_for_edits.py --deployment acme-corp [--dry-run]

--dry-run here means "compute and print the diffs, don't call the learning
agent or write style_notes.md" — a way to sanity-check detection before
trusting it to revise the style guide.
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, create_sdk_mcp_server, query

import sequencing
from billing import SubscriptionInactive, require_active_subscription
from deployment import Deployment, load_deployment
from clients.mailbox_client import MailboxClient, normalize_for_diff
from contacts import ContactVault
from pii import scrub_text
from tools.fs_tools import build_learning_fs_tools
from agents import learning_agent

MAX_DIFF_LOG_ENTRIES = 200


def _load_audit(deployment: Deployment) -> list[dict[str, Any]]:
    raw = deployment.store.read_text(deployment.state_key("drafts_audit.json"))
    return json.loads(raw) if raw else []


def _save_audit(deployment: Deployment, entries: list[dict[str, Any]]) -> None:
    deployment.store.write_text(deployment.state_key("drafts_audit.json"), json.dumps(entries, indent=2))


def _append_diff_log(deployment: Deployment, entry: dict[str, Any]) -> None:
    deployment.store.append_line(
        deployment.state_key("learning", "edit_diffs_log.jsonl"), json.dumps(entry), max_lines=MAX_DIFF_LOG_ENTRIES
    )


def _compute_diff(before: str, after: str) -> str:
    return "\n".join(
        difflib.unified_diff(
            before.splitlines(), after.splitlines(), fromfile="agent_draft", tofile="human_final", lineterm=""
        )
    )


async def _invoke_learning_agent(deployment: Deployment, entry: dict[str, Any], after_text: str, diff: str) -> None:
    options = ClaudeAgentOptions(
        tools=[],  # disable ALL built-in tools — this stage cannot touch a draft, only style_notes.md
        system_prompt=learning_agent.SYSTEM_PROMPT,
        allowed_tools=learning_agent.ALLOWED_TOOLS,
        mcp_servers={"fs": create_sdk_mcp_server(name="fs", tools=build_learning_fs_tools(deployment))},
        permission_mode="bypassPermissions",
        model=deployment.models.get("learning"),
        max_turns=20,
    )
    # No-PII rule: everything below is already depersonalized (placeholders) and scrubbed.
    prompt = (
        f"A human edit was detected for a draft to a "
        f"{entry.get('stakeholder_title') or 'contact'} at {entry['account_name']}.\n\n"
        f"BEFORE (agent-generated):\n{entry['_before_safe']}\n\n"
        f"AFTER (human final, {entry['status']}):\n{after_text}\n\n"
        f"DIFF:\n{diff}\n\n"
        f"Decide what this teaches, then update style_notes.md accordingly."
    )
    async for _ in query(prompt=prompt, options=options):
        pass


async def poll_once(deployment: Deployment, dry_run: bool) -> int:
    audit = _load_audit(deployment)
    # Built lazily: a fresh deployment with no open drafts yet shouldn't
    # need working mailbox OAuth just to run this cron job.
    mailbox = MailboxClient(deployment) if any(e.get("status") == "open" for e in audit) else None
    vault = ContactVault(deployment)
    learned = 0

    def safe(text: str, entry: dict[str, Any]) -> str:
        contact = vault.get(entry.get("stakeholder_id")) or {"email": entry.get("to", ""),
                                                             "name": entry.get("stakeholder_name", "")}
        return scrub_text(ContactVault.depersonalize(text, contact, getattr(deployment, 'sender_names', [])))

    for entry in audit:
        if entry.get("status") != "open":
            continue

        after_text = mailbox.get_finalized_text(entry["tracking_id"], entry["draft_ref"])
        source = "sent"

        if after_text is not None and not dry_run:
            # Genuinely sent (not the stable-but-still-open signal below) —
            # this is what starts a sequence lead's reply-check timer, if
            # this draft is part of one. No-op if it isn't. Gated on
            # dry_run for the same reason audit status updates are below:
            # --dry-run previews detection without mutating persistent state.
            sequencing.mark_sent(deployment, entry["tracking_id"])
            entry["sent_at"] = datetime.now(timezone.utc).isoformat()  # dashboard.py reads this

        if after_text is None and deployment.learning.enable_stable_draft_signal:
            current = mailbox.get_current_draft_text(entry["draft_ref"])
            before_norm = normalize_for_diff(entry["before_text"])
            if current is not None and current != before_norm:
                last_seen = entry.get("changed_first_seen_at")
                now = datetime.now(timezone.utc)
                if last_seen is None:
                    entry["changed_first_seen_at"] = now.isoformat()
                    continue
                settle = timedelta(hours=deployment.learning.stable_draft_settle_hours)
                if now - datetime.fromisoformat(last_seen) >= settle:
                    after_text = current
                    source = "stable_unsent"

        if after_text is None:
            continue

        before_norm = safe(normalize_for_diff(entry["before_text"]), entry)
        after_text = safe(after_text, entry)
        if after_text == before_norm:
            entry["status"] = f"unchanged_{source}"
            continue

        diff = _compute_diff(before_norm, after_text)
        entry["_before_safe"] = before_norm
        print(f"[poll_for_edits] {entry.get('title') or entry.get('stakeholder_title') or 'contact'} ({entry['account_name']}): "
              f"detected {source} edit\n{diff}\n")

        if not dry_run:
            await _invoke_learning_agent(deployment, entry, after_text, diff)
            _append_diff_log(deployment, {
                "tracking_id": entry["tracking_id"],
                "detected_at": datetime.now(timezone.utc).isoformat(),
                "source": source,
                "account_name": entry["account_name"],
                "diff": diff,
            })
            entry["status"] = f"learned_{source}"
            learned += 1
        else:
            entry_preview_status = f"would_learn_{source}"
            print(f"[poll_for_edits] --dry-run: not invoking learning_agent (status would become {entry_preview_status})")

    for entry in audit:
        entry.pop("_before_safe", None)
    _save_audit(deployment, audit)
    return learned


def main() -> None:
    parser = argparse.ArgumentParser(description="Poll for human edits to drafts and learn from them")
    parser.add_argument("--deployment", required=True)
    parser.add_argument("--dry-run", action="store_true", help="Detect and print diffs, don't update style_notes.md")
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
            print(f"[poll_for_edits] BILLING GATE: {exc}")
            raise SystemExit(1)

    learned = asyncio.run(poll_once(deployment, args.dry_run))
    print(f"[poll_for_edits] done. {learned} edit(s) learned from this run.")


if __name__ == "__main__":
    main()
