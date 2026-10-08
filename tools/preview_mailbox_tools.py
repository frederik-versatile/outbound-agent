"""Dry-run stand-in for mailbox_tools.py: same tool name and schema as
mailbox_create_draft, so drafting_agent's prompt and allowed_tools never
change between dry-run and live — only what's behind the tool changes.

Writes composed emails to state/<deployment_id>/runs/<run_id>/drafts_preview/*.md
instead of calling any mailbox API at all. This is what safety.dry_run_default
uses by default for every new deployment until a human explicitly disables it.

Also writes run_dir/drafts_created.json, same schema as the live path, so
the suppression/sequence-seeding logic in orchestrator.py is exercisable
end-to-end in dry-run — see tests/test_apollo_client.py-style offline tests.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from claude_agent_sdk import tool, create_sdk_mcp_server

from contacts import ContactVault
from tools.mailbox_tools import CREATE_DRAFT_SCHEMA, append_drafts_created, prepare_draft


def build_preview_mailbox_tools(
    run_dir: Path, max_drafts_per_run: int, *, sequence_step: int = 0,
    vault: ContactVault | None = None, fixed_contact: dict[str, Any] | None = None,
) -> list:
    preview_dir = run_dir / "drafts_preview"
    preview_dir.mkdir(parents=True, exist_ok=True)
    drafts_created = {"count": 0}

    @tool(
        "mailbox_create_draft",
        "DRY RUN: writes the composed email to a local preview file instead of any real mailbox. "
        "This never sends and never touches a real inbox.",
        CREATE_DRAFT_SCHEMA,
    )
    async def create_draft(args: dict[str, Any]) -> dict[str, Any]:
        if drafts_created["count"] >= max_drafts_per_run:
            return {
                "content": [{
                    "type": "text",
                    "text": f"ERROR: max_drafts_per_run ({max_drafts_per_run}) reached for this run.",
                }],
                "is_error": True,
            }
        prepared = prepare_draft(args, vault, fixed_contact)
        if isinstance(prepared, str):
            return {"content": [{"type": "text", "text": prepared}], "is_error": True}
        contact, merged_subject, merged_body = prepared

        drafts_created["count"] += 1
        n = drafts_created["count"]
        tracking_id = str(uuid.uuid4())
        person_id = str(args.get("stakeholder_id") or contact.get("person_id") or "contact")
        preview_path = preview_dir / f"{n:03d}_{person_id}.md"
        # The preview file is for the human reviewer, so it shows the merged email.
        preview_path.write_text(
            f"# DRY RUN — not sent, not drafted in any real mailbox\n\n"
            f"**To:** {contact['email']}  \n"
            f"**Account:** {args['account_name']}  \n"
            f"**Stakeholder:** {contact.get('name', '')} ({contact.get('title', '')})  \n"
            f"**Subject:** {merged_subject}\n\n"
            f"---\n\n{merged_body}\n"
        )

        append_drafts_created(run_dir, {
            "tracking_id": tracking_id,
            "draft_ref": f"preview-{tracking_id}",
            "provider": "preview",
            "thread_id": f"preview-thread-{tracking_id}",
            "to": contact["email"],
            "subject": args["subject"],
            "account_id": args.get("account_id"),
            "account_name": args["account_name"],
            "stakeholder_id": person_id,
            "stakeholder_name": contact.get("name", ""),
            "stakeholder_title": contact.get("title", ""),
            "sequence_step": sequence_step,
        })

        return {"content": [{
            "type": "text",
            "text": f"[DRY RUN] Preview {n:03d} written. "
                    f"{n}/{max_drafts_per_run} drafts used this run.",
        }]}

    return [create_draft]


def preview_mailbox_server(run_dir: Path, max_drafts_per_run: int, *, sequence_step: int = 0,
                           vault: ContactVault | None = None):
    return create_sdk_mcp_server(
        name="mailbox",
        tools=build_preview_mailbox_tools(run_dir, max_drafts_per_run, sequence_step=sequence_step, vault=vault),
    )
