"""@tool wrapper exposing exactly one mailbox-write capability to any agent:
mailbox_create_draft. There is deliberately no mailbox_send_* tool defined
anywhere in this file — see clients/gmail_client.py and clients/outlook_client.py,
which don't even implement a send method for this to wrap.

max_drafts_per_run is enforced here via a closure-scoped counter, in addition
to orchestrator.py truncating the stakeholder list before drafting_agent runs
at all — the same belt-and-suspenders pattern as apollo_tools' credit guard.

Thread continuity (thread_id/in_reply_to) and which sequence step this is
are Python-supplied closure defaults, NOT agent-facing tool parameters —
the calling code (orchestrator.py for step 0, advance_sequences.py for
later steps) always knows exactly which lead/thread it's drafting for, so
there's no reason to trust the model to echo that back correctly.

No-PII rule: the agent never sees or supplies a recipient's name or address. It passes a
stakeholder_id (Apollo person_id) and writes subject/body with {{first_name}}-style placeholders;
prepare_draft() looks the contact up in the code-only ContactVault, fills the placeholders, and only
the merged text goes to the mailbox. The audit trail keeps the placeholder version as before_text,
so the follow-up and learning stages only ever see placeholder text.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from claude_agent_sdk import tool, create_sdk_mcp_server

from agents.style_rules import EM_DASH
from clients.mailbox_client import MailboxClient
from contacts import ContactVault, PlaceholderError
from deployment import Deployment

CREATE_DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "stakeholder_id": {"type": "string", "description": "The stakeholder's person_id. The code looks up "
                                                            "their name and email; you never handle them."},
        "subject": {"type": "string", "description": "May use {{first_name}}, {{last_name}}, {{full_name}}"},
        "body_text": {"type": "string", "description": "Use {{first_name}} etc. for the recipient's name; "
                                                       "never write a name or email address yourself."},
        "account_id": {"type": "string", "description": "Apollo org id, for the audit trail"},
        "account_name": {"type": "string"},
    },
    "required": ["subject", "body_text", "account_name"],
}


class DraftLimitExceeded(RuntimeError):
    pass


def em_dash_error(subject: str, body_text: str) -> str | None:
    """Global rule, enforced here so a missed instruction can't quietly
    slip through as a live draft — see agents/style_rules.py. Returns an
    error message for the tool to surface if either field contains an
    em-dash, else None."""
    if EM_DASH in subject or EM_DASH in body_text:
        return (
            f"ERROR: subject or body_text contains an em-dash ({EM_DASH}), which is never "
            f"allowed (see the global rules in your system prompt). Rewrite the sentence "
            f"without it — usually as two sentences or with a comma — and call "
            f"mailbox_create_draft again."
        )
    return None


def prepare_draft(
    args: dict[str, Any], vault: ContactVault | None, fixed_contact: dict[str, Any] | None
) -> tuple[dict[str, Any], str, str] | str:
    """Validate the placeholder text, resolve the contact, and return (contact, merged_subject,
    merged_body) — or an error message for the agent (which never contains personal data)."""
    error = em_dash_error(args["subject"], args["body_text"])
    if error:
        return error
    try:
        ContactVault.check_template(args["subject"], args["body_text"])
    except PlaceholderError as exc:
        return f"ERROR: {exc}"
    contact = fixed_contact or (vault.get(args.get("stakeholder_id")) if vault else None)
    if not contact or not contact.get("email"):
        return ("ERROR: unknown stakeholder_id, or no verified email held for it. Use a person_id from "
                "read_stakeholders.")
    return (contact, ContactVault.merge(args["subject"], contact), ContactVault.merge(args["body_text"], contact))


def _append_audit(deployment: Deployment, entry: dict[str, Any]) -> None:
    audit_key = deployment.state_key("drafts_audit.json")
    raw = deployment.store.read_text(audit_key)
    existing = json.loads(raw) if raw else []
    existing.append(entry)
    deployment.store.write_text(audit_key, json.dumps(existing, indent=2))


def append_drafts_created(run_dir: Path, entry: dict[str, Any]) -> None:
    path = run_dir / "drafts_created.json"
    existing = json.loads(path.read_text()) if path.exists() else []
    existing.append(entry)
    path.write_text(json.dumps(existing, indent=2))


def build_mailbox_tools(
    mailbox: MailboxClient,
    deployment: Deployment,
    max_drafts_per_run: int,
    run_dir: Path,
    *,
    thread_id: str | None = None,
    in_reply_to: str | None = None,
    sequence_step: int = 0,
    vault: ContactVault | None = None,
    fixed_contact: dict[str, Any] | None = None,
) -> list:
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

        prepared = prepare_draft(args, vault, fixed_contact)
        if isinstance(prepared, str):
            return {"content": [{"type": "text", "text": prepared}], "is_error": True}
        contact, merged_subject, merged_body = prepared

        tracking_id = str(uuid.uuid4())
        result = mailbox.create_draft(
            to=contact["email"], subject=merged_subject, body_text=merged_body, tracking_id=tracking_id,
            thread_id=thread_id, in_reply_to=in_reply_to,
        )
        drafts_created["count"] += 1

        # Code-only records: "to" and "stakeholder_name" are for the sequence tracker and the human
        # dashboard; subject/before_text keep the placeholder version for any later agent stage.
        common = {
            "tracking_id": tracking_id,
            "draft_ref": result["draft_ref"],
            "provider": result["provider"],
            "thread_id": result.get("thread_id"),
            "to": contact["email"],
            "subject": args["subject"],
            "account_id": args.get("account_id"),
            "account_name": args["account_name"],
            "stakeholder_id": args.get("stakeholder_id") or contact.get("person_id"),
            "stakeholder_name": contact.get("name", ""),
            "stakeholder_title": contact.get("title", ""),
            "sequence_step": sequence_step,
        }

        _append_audit(deployment, {
            **common,
            "before_text": args["body_text"],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "open",
        })
        append_drafts_created(run_dir, common)

        return {"content": [{
            "type": "text",
            "text": f"Draft created (tracking_id={tracking_id}, draft_ref={result['draft_ref']}). "
                    f"{drafts_created['count']}/{max_drafts_per_run} drafts used this run.",
        }]}

    return [create_draft]


def mailbox_server(
    mailbox: MailboxClient,
    deployment: Deployment,
    max_drafts_per_run: int,
    run_dir: Path,
    *,
    thread_id: str | None = None,
    in_reply_to: str | None = None,
    sequence_step: int = 0,
    vault: ContactVault | None = None,
    fixed_contact: dict[str, Any] | None = None,
):
    return create_sdk_mcp_server(
        name="mailbox",
        tools=build_mailbox_tools(
            mailbox, deployment, max_drafts_per_run, run_dir,
            thread_id=thread_id, in_reply_to=in_reply_to, sequence_step=sequence_step,
            vault=vault, fixed_contact=fixed_contact,
        ),
    )
