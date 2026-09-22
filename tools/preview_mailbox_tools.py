"""Dry-run stand-in for mailbox_tools.py: same tool name and schema as
mailbox_create_draft, so drafting_agent's prompt and allowed_tools never
change between dry-run and live — only what's behind the tool changes.

Writes composed emails to state/<deployment_id>/runs/<run_id>/drafts_preview/*.md
instead of calling any mailbox API at all. This is what safety.dry_run_default
uses by default for every new deployment until a human explicitly disables it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from claude_agent_sdk import tool, create_sdk_mcp_server

from tools.mailbox_tools import CREATE_DRAFT_SCHEMA


def build_preview_mailbox_tools(preview_dir: Path, max_drafts_per_run: int) -> list:
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
        drafts_created["count"] += 1
        n = drafts_created["count"]
        safe_name = args["stakeholder_name"].replace("/", "-").replace(" ", "_")
        preview_path = preview_dir / f"{n:03d}_{safe_name}.md"
        preview_path.write_text(
            f"# DRY RUN — not sent, not drafted in any real mailbox\n\n"
            f"**To:** {args['to']}  \n"
            f"**Account:** {args['account_name']}  \n"
            f"**Stakeholder:** {args['stakeholder_name']}  \n"
            f"**Subject:** {args['subject']}\n\n"
            f"---\n\n{args['body_text']}\n"
        )
        return {"content": [{
            "type": "text",
            "text": f"[DRY RUN] Preview written to {preview_path}. "
                    f"{n}/{max_drafts_per_run} drafts used this run.",
        }]}

    return [create_draft]


def preview_mailbox_server(preview_dir: Path, max_drafts_per_run: int):
    return create_sdk_mcp_server(
        name="mailbox", tools=build_preview_mailbox_tools(preview_dir, max_drafts_per_run)
    )
