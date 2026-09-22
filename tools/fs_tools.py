"""@tool wrappers giving each stage's subagent read/write access to exactly
the files it needs — the ICP/positioning/style docs it reads, and the
run-scoped JSON output it writes. Built as a factory per run so each call
closes over that run's deployment + run_dir without any global state.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from claude_agent_sdk import tool

from deployment import Deployment


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def _read_json(path: Path) -> Any:
    if not path.exists():
        return None
    return json.loads(path.read_text())


def build_prioritization_fs_tools(deployment: Deployment, run_dir: Path) -> list:
    @tool("read_icp_doc", "Read this deployment's ICP criteria doc.", {})
    async def read_icp_doc(args: dict[str, Any]) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": deployment.read_icp_doc()}]}

    @tool(
        "write_scored_accounts",
        "Write the final list of scored/ranked candidate accounts. Overwrites any previous write this run.",
        {"type": "object", "properties": {"accounts": {"type": "array"}}, "required": ["accounts"]},
    )
    async def write_scored_accounts(args: dict[str, Any]) -> dict[str, Any]:
        accounts = args["accounts"]
        _write_json(run_dir / "accounts_scored.json", accounts)
        return {"content": [{"type": "text", "text": f"Wrote {len(accounts)} scored accounts."}]}

    return [read_icp_doc, write_scored_accounts]


def build_discovery_fs_tools(deployment: Deployment, run_dir: Path) -> list:
    @tool("read_scored_accounts", "Read the accounts scored by the prioritization stage.", {})
    async def read_scored_accounts(args: dict[str, Any]) -> dict[str, Any]:
        accounts = _read_json(run_dir / "accounts_scored.json") or []
        return {"content": [{"type": "text", "text": json.dumps(accounts)}]}

    @tool(
        "write_stakeholders",
        "Write the final list of discovered stakeholders with verified emails. Overwrites any previous write this run.",
        {"type": "object", "properties": {"stakeholders": {"type": "array"}}, "required": ["stakeholders"]},
    )
    async def write_stakeholders(args: dict[str, Any]) -> dict[str, Any]:
        stakeholders = args["stakeholders"]
        _write_json(run_dir / "stakeholders.json", stakeholders)
        return {"content": [{"type": "text", "text": f"Wrote {len(stakeholders)} stakeholders."}]}

    return [read_scored_accounts, write_stakeholders]


def build_drafting_fs_tools(deployment: Deployment, run_dir: Path) -> list:
    @tool("read_stakeholders", "Read the stakeholders found by the discovery stage.", {})
    async def read_stakeholders(args: dict[str, Any]) -> dict[str, Any]:
        stakeholders = _read_json(run_dir / "stakeholders.json") or []
        return {"content": [{"type": "text", "text": json.dumps(stakeholders)}]}

    @tool("read_scored_accounts", "Read the accounts scored by the prioritization stage (for match rationale).", {})
    async def read_scored_accounts(args: dict[str, Any]) -> dict[str, Any]:
        accounts = _read_json(run_dir / "accounts_scored.json") or []
        return {"content": [{"type": "text", "text": json.dumps(accounts)}]}

    @tool("read_positioning_doc", "Read this deployment's positioning/value-prop doc.", {})
    async def read_positioning_doc(args: dict[str, Any]) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": deployment.read_positioning_doc()}]}

    @tool(
        "read_style_notes",
        "Read the current learned style guide for this deployment. Empty string if none exists yet.",
        {},
    )
    async def read_style_notes(args: dict[str, Any]) -> dict[str, Any]:
        text = deployment.store.read_text(deployment.state_key("learning", "style_notes.md")) or ""
        return {"content": [{"type": "text", "text": text}]}

    return [read_stakeholders, read_scored_accounts, read_positioning_doc, read_style_notes]


def build_learning_fs_tools(deployment: Deployment) -> list:
    style_notes_key = deployment.state_key("learning", "style_notes.md")
    diff_log_key = deployment.state_key("learning", "edit_diffs_log.jsonl")

    @tool("read_style_notes", "Read the current style guide, to be revised.", {})
    async def read_style_notes(args: dict[str, Any]) -> dict[str, Any]:
        text = deployment.store.read_text(style_notes_key) or ""
        return {"content": [{"type": "text", "text": text}]}

    @tool(
        "write_style_notes",
        "Replace the style guide with a revised version (merge/prune, not pure append).",
        {"content": str},
    )
    async def write_style_notes(args: dict[str, Any]) -> dict[str, Any]:
        deployment.store.write_text(style_notes_key, args["content"])
        return {"content": [{"type": "text", "text": "style_notes.md updated."}]}

    @tool(
        "read_recent_diff_log",
        "Read the last N entries of the bounded raw edit-diff log, for short-term context.",
        {"limit": int},
    )
    async def read_recent_diff_log(args: dict[str, Any]) -> dict[str, Any]:
        limit = args.get("limit", 20)
        raw = deployment.store.read_text(diff_log_key)
        if not raw:
            return {"content": [{"type": "text", "text": "[]"}]}
        lines = raw.splitlines()[-limit:]
        return {"content": [{"type": "text", "text": "[" + ",".join(lines) + "]"}]}

    return [read_style_notes, write_style_notes, read_recent_diff_log]
