"""@tool wrapper exposing exactly one mailbox-write capability to any agent:
mailbox_create_draft. There is deliberately no mailbox_send_* tool defined
anywhere in this file — see clients/gmail_client.py and clients/outlook_client.py,
which don't even implement a send method for this to wrap.

max_drafts_per_run is enforced here via a closure-scoped counter, in addition
to orchestrator.py truncating the stakeholder list before drafting_agent runs
at all — the same belt-and-suspenders pattern as apollo_tools' credit guard.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from claude_agent_sdk import tool, create_sdk_mcp_server

from clients.mailbox_client import MailboxClient
from deployment import Deployment

CREATE_DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "to": {"type": "string", "description": "Recipient email address"},
        "subject": {"type": "string"},
        "body_text": {"type": "string"},
        "account_id": {"type": "string", "description": "Apollo org id, for the audit trail"},
        "account_name": {"type": "string"},
        "stakeholder_id": {"type": "string", "description": "Apollo person id, for the audit trail"},
        "stakeholder_name": {"type": "string"},
    },
    "required": ["to", "subject", "body_text", "account_name", "stakeholder_name"],
}


class DraftLimitExceeded(RuntimeError):
    pass


def _append_audit(deployment: Deployment, entry: dict[str, Any]) -> None:
    audit_key = deployment.state_key("drafts_audit.json")
    raw = deployment.store.read_text(audit_key)
    existing = json.loads(raw) if raw else []
    existing.append(entry)
    deployment.store.write_text(audit_key, json.dumps(existing, indent=2))


def build_mailbox_tools(mailbox: MailboxClient, deployment: Deployment, max_drafts_per_run: int) -> list:
    drafts_created = {"count": 0}

    @tool(
        "mailbox_create_draft",
        "Create a draft email in the configured mailbox (Gmail or Outlook). This NEVER sends — "
        "it only creates a draft for a human to review, edit, and send themselves.",
        CREATE_DRAFT_SCHEMA,
    )
    async def create_draft(args: dict[str, Any]) -> dict[str, Any]:
        if drafts_created["count"] >= max_drafts_per_run:
            return {
                "content": [{
                    "type": "text",
                    "text": f"ERROR: max_drafts_per_run ({max_drafts_per_run}) reached for this run. "
                            f"No more drafts will be created.",
                }],
                "is_error": True,
            }

        tracking_id = str(uuid.uuid4())
        result = mailbox.create_draft(
            to=args["to"], subject=args["subject"], body_text=args["body_text"], tracking_id=tracking_id
        )
        drafts_created["count"] += 1

        _append_audit(deployment, {
            "tracking_id": tracking_id,
            "draft_ref": result["draft_ref"],
            "provider": result["provider"],
            "to": args["to"],
            "subject": args["subject"],
            "before_text": args["body_text"],
            "account_id": args.get("account_id"),
            "account_name": args["account_name"],
            "stakeholder_id": args.get("stakeholder_id"),
            "stakeholder_name": args["stakeholder_name"],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "open",
        })

        return {"content": [{
            "type": "text",
            "text": f"Draft created (tracking_id={tracking_id}, draft_ref={result['draft_ref']}). "
                    f"{drafts_created['count']}/{max_drafts_per_run} drafts used this run.",
        }]}

    return [create_draft]


def mailbox_server(mailbox: MailboxClient, deployment: Deployment, max_drafts_per_run: int):
    return create_sdk_mcp_server(
        name="mailbox", tools=build_mailbox_tools(mailbox, deployment, max_drafts_per_run)
    )
