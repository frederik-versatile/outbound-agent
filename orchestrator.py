"""CLI entrypoint for the live pipeline: prioritize -> discover -> draft.

    python orchestrator.py run --deployment acme-corp [--dry-run] [--use-fixtures]
                                [--resume-from discovery --run-id 20260922-101500]

Each stage is invoked as its own query() call with its own system prompt,
tool allowlist, and model — not delegated to autonomously by one top-level
agent — so that the safety caps below are enforced by this Python code
between stages, not by trusting an LLM's own restraint. See the plan's
"Pipeline stages" section for the reasoning.

Every stage writes its output to state/<deployment>/runs/<run_id>/ before the
next stage starts, so a crash is resumable from disk.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, create_sdk_mcp_server, query

import sequencing
from billing import SubscriptionInactive, require_active_subscription
from deployment import Deployment, load_deployment
from clients.apollo_client import ApolloClient
from clients.mailbox_client import MailboxClient
from clients.sheets_client import SheetsClient
from contacts import ContactVault
from tools.fs_tools import (
    build_prioritization_fs_tools,
    build_discovery_fs_tools,
    build_drafting_fs_tools,
)
from tools.apollo_tools import apollo_server
from tools.mailbox_tools import mailbox_server
from tools.preview_mailbox_tools import preview_mailbox_server
from agents import prioritization_agent, discovery_agent, drafting_agent

STAGES = ["prioritization", "discovery", "drafting"]


def _new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _run_log_writer(deployment: Deployment, run_id: str):
    log_key = deployment.state_key("run_log.jsonl")

    def write(stage: str, event: str, detail: str = "") -> None:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "run_id": run_id,
            "stage": stage,
            "event": event,
            "detail": detail[:2000],
        }
        deployment.store.append_line(log_key, json.dumps(entry), max_lines=5000)

    return write


async def _run_stage(prompt: str, options: ClaudeAgentOptions, log, stage: str) -> None:
    # Per-message detail goes to stdout only (Render captures cron job stdout
    # natively) rather than through log()/deployment.store: log() does a
    # read-modify-write against the durable store on every call, and a full
    # message stream would make that an expensive, mostly-redundant append
    # per message. The durable run log stays to stage-level transitions.
    log(stage, "started")
    async for message in query(prompt=prompt, options=options):
        print(f"[{stage}] {str(message)[:500]}")
    log(stage, "finished")


def _normalize_domain(raw: str) -> str:
    domain = raw.strip().lower()
    for prefix in ("https://", "http://", "www."):
        if domain.startswith(prefix):
            domain = domain[len(prefix):]
    return domain.split("/")[0]


def cross_reference_hire_sheet(deployment: Deployment, accounts: list[dict[str, Any]], log) -> list[dict[str, Any]]:
    """Boosts (never introduces) accounts that also appear on the customer's
    hire-sheet — see deployment.py's HireSheetConfig docstring. Never the
    primary source of accounts; a read/parse failure here logs a warning
    and leaves accounts unmodified rather than failing the whole run over
    what's meant to be a secondary signal."""
    if not deployment.hire_sheet.enabled or not deployment.hire_sheet.spreadsheet_id:
        return accounts

    try:
        client = SheetsClient(deployment)
        rows = client.read_range(deployment.hire_sheet.spreadsheet_id, deployment.hire_sheet.range)
    except Exception as exc:
        log("prioritization", "hire_sheet_read_failed", str(exc))
        return accounts

    sheet_domains: set[str] = set()
    for row in rows:
        if len(row) > deployment.hire_sheet.domain_column and row[deployment.hire_sheet.domain_column]:
            sheet_domains.add(_normalize_domain(row[deployment.hire_sheet.domain_column]))

    matched = 0
    for account in accounts:
        account_domain = _normalize_domain(account.get("domain", ""))
        if account_domain and account_domain in sheet_domains:
            account["score"] = min(100, account.get("score", 0) + deployment.hire_sheet.score_boost)
            account["rationale"] = (
                account.get("rationale", "").rstrip(". ")
                + f". Also confirmed on the customer's hire-signal sheet (+{deployment.hire_sheet.score_boost})."
            )
            matched += 1

    log("prioritization", "hire_sheet_matches", str(matched))
    return accounts


async def run_prioritization(deployment: Deployment, run_dir: Path, apollo: ApolloClient, log,
                             vault: ContactVault | None = None) -> list[dict[str, Any]]:
    options = ClaudeAgentOptions(
        tools=[],  # disable ALL built-in tools (Bash, Read, Write, WebFetch, ...) — see module docstring
        system_prompt=prioritization_agent.SYSTEM_PROMPT,
        allowed_tools=prioritization_agent.ALLOWED_TOOLS,
        mcp_servers={
            "fs": create_sdk_mcp_server(name="fs", tools=build_prioritization_fs_tools(deployment, run_dir)),
            "apollo": apollo_server(apollo, vault),
        },
        permission_mode="bypassPermissions",
        model=deployment.models.get("prioritization"),
        max_turns=40,
    )
    prompt = (
        f"Score accounts for this deployment against its ICP doc. "
        f"Return at most {deployment.safety.max_accounts_per_run} accounts, "
        f"ranked highest score first."
    )
    await _run_stage(prompt, options, log, "prioritization")

    result_path = run_dir / "accounts_scored.json"
    if not result_path.exists():
        raise RuntimeError("prioritization stage finished without writing accounts_scored.json")
    accounts = json.loads(result_path.read_text())

    # Cross-reference BEFORE the cap, since a hire-sheet match can boost an
    # account's score enough to make the cut.
    accounts = cross_reference_hire_sheet(deployment, accounts, log)

    # Python-level cap, independent of whatever the agent already did.
    accounts = sorted(accounts, key=lambda a: a.get("score", 0), reverse=True)
    accounts = accounts[: deployment.safety.max_accounts_per_run]
    result_path.write_text(json.dumps(accounts, indent=2))
    return accounts


async def run_discovery(deployment: Deployment, run_dir: Path, apollo: ApolloClient, log,
                        vault: ContactVault | None = None) -> list[dict[str, Any]]:
    vault = vault or ContactVault(deployment)
    options = ClaudeAgentOptions(
        tools=[],  # disable ALL built-in tools — this stage gets only its MCP allowlist
        system_prompt=discovery_agent.SYSTEM_PROMPT,
        allowed_tools=discovery_agent.ALLOWED_TOOLS,
        mcp_servers={
            "fs": create_sdk_mcp_server(name="fs", tools=build_discovery_fs_tools(deployment, run_dir, vault)),
            "apollo": apollo_server(apollo, vault),
        },
        permission_mode="bypassPermissions",
        model=deployment.models.get("discovery"),
        max_turns=60,
    )
    prompt = (
        f"Find stakeholders for the scored accounts. Target titles: "
        f"{deployment.target_titles}. Target seniorities: {deployment.target_seniorities}. "
        f"At most {deployment.safety.max_contacts_per_account} stakeholders per account. "
        f"At most {deployment.safety.max_total_enrich_calls_per_run} total Apollo enrichment "
        f"calls this run — stop before exceeding that, a partial result is fine."
    )
    await _run_stage(prompt, options, log, "discovery")

    result_path = run_dir / "stakeholders.json"
    if not result_path.exists():
        raise RuntimeError("discovery stage finished without writing stakeholders.json")
    stakeholders = json.loads(result_path.read_text())

    # Python-level caps, independent of the agent's own bookkeeping.
    by_account: dict[str, list[dict[str, Any]]] = {}
    for s in stakeholders:
        by_account.setdefault(s.get("account_org_id", ""), []).append(s)
    capped: list[dict[str, Any]] = []
    for _, people in by_account.items():
        capped.extend(people[: deployment.safety.max_contacts_per_account])

    # Suppress anyone already mid-sequence, already replied, or still inside
    # the recycle cooldown — without this, the pipeline would draft a fresh
    # cold opener to the SAME person every time it runs. See sequencing.py.
    sequence_entries = sequencing.load_sequences(deployment)
    before_suppression = len(capped)
    capped = [s for s in capped
              if not sequencing.is_suppressed(sequence_entries, vault.email(s.get("person_id")))]
    suppressed_count = before_suppression - len(capped)

    result_path.write_text(json.dumps(capped, indent=2))

    log("discovery", "apollo_credits_used", str(apollo.credits_used_this_run()))
    log("discovery", "suppressed_existing_leads", str(suppressed_count))
    return capped


async def run_drafting(
    deployment: Deployment, run_dir: Path, dry_run: bool, mailbox_override: str | None, log,
    vault: ContactVault | None = None,
) -> None:
    vault = vault or ContactVault(deployment)
    if dry_run:
        mcp_mailbox = preview_mailbox_server(run_dir, deployment.safety.max_drafts_per_run, sequence_step=0,
                                             vault=vault)
    else:
        mailbox = MailboxClient(deployment, provider_override=mailbox_override)
        mcp_mailbox = mailbox_server(mailbox, deployment, deployment.safety.max_drafts_per_run, run_dir,
                                     sequence_step=0, vault=vault)

    options = ClaudeAgentOptions(
        tools=[],  # disable ALL built-in tools — mailbox_create_draft is its only write capability
        system_prompt=drafting_agent.SYSTEM_PROMPT,
        allowed_tools=drafting_agent.ALLOWED_TOOLS,
        mcp_servers={
            "fs": create_sdk_mcp_server(name="fs", tools=build_drafting_fs_tools(deployment, run_dir)),
            "mailbox": mcp_mailbox,
        },
        permission_mode="bypassPermissions",
        model=deployment.models.get("drafting"),
        max_turns=80,
    )
    prompt = (
        f"Draft one personalized outreach email per stakeholder. "
        f"At most {deployment.safety.max_drafts_per_run} drafts this run."
        + (" This is a DRY RUN: drafts are written to local preview files only." if dry_run else "")
    )
    await _run_stage(prompt, options, log, "drafting")

    drafts_created_path = run_dir / "drafts_created.json"
    drafts_created = json.loads(drafts_created_path.read_text()) if drafts_created_path.exists() else []
    sequencing.seed_step_zero(deployment, drafts_created)
    log("drafting", "sequences_seeded", str(len(drafts_created)))


async def main_async(args: argparse.Namespace) -> None:
    deployment = load_deployment(args.deployment)

    if not args.skip_billing_check:
        require_active_subscription(deployment)  # raises SubscriptionInactive; caller exits non-zero

    dry_run = args.dry_run or (deployment.safety.dry_run_default and not args.live)

    run_id = args.run_id or _new_run_id()
    run_dir = deployment.run_dir(run_id)
    log = _run_log_writer(deployment, run_id)

    apollo = ApolloClient(
        api_key=deployment.apollo_api_key if not args.use_fixtures else "fixture-mode",
        max_credits_per_run=deployment.safety.max_total_enrich_calls_per_run,
        mode="fixture" if args.use_fixtures else "live",
        fixtures_dir=Path(args.fixtures_dir) if args.fixtures_dir else None,
    )

    vault = ContactVault(deployment)  # code-only names/emails; agents get person_ids (no-PII rule)

    start_stage = args.resume_from or "prioritization"
    stage_index = STAGES.index(start_stage)

    print(f"[outbound-agent] deployment={args.deployment} run_id={run_id} "
          f"dry_run={dry_run} stages={STAGES[stage_index:]}")

    if stage_index <= STAGES.index("prioritization"):
        accounts = await run_prioritization(deployment, run_dir, apollo, log, vault)
        print(f"[outbound-agent] prioritization: {len(accounts)} accounts")

    if stage_index <= STAGES.index("discovery"):
        stakeholders = await run_discovery(deployment, run_dir, apollo, log, vault)
        print(f"[outbound-agent] discovery: {len(stakeholders)} stakeholders")

    if stage_index <= STAGES.index("drafting"):
        await run_drafting(deployment, run_dir, dry_run, args.mailbox_override, log, vault)
        print(f"[outbound-agent] drafting: done. "
              f"{'Preview files in ' + str(run_dir / 'drafts_preview') if dry_run else 'Drafts created in mailbox.'}")

    print(f"[outbound-agent] run complete: {run_dir}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Outbound agent pipeline")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Run the prioritize -> discover -> draft pipeline")
    run_p.add_argument("--deployment", required=True)
    run_p.add_argument("--dry-run", action="store_true", help="Force dry run even if config default is live")
    run_p.add_argument("--live", action="store_true", help="Force live mailbox writes even if config default is dry-run")
    run_p.add_argument("--use-fixtures", action="store_true", help="Replay recorded Apollo fixtures, zero live API calls")
    run_p.add_argument(
        "--fixtures-dir", default=None,
        help="Directory of Apollo fixture JSON files to replay, overriding the default tests/fixtures/. "
             "Only meaningful with --use-fixtures.",
    )
    run_p.add_argument("--resume-from", choices=STAGES, default=None)
    run_p.add_argument("--run-id", default=None, help="Required when using --resume-from")
    run_p.add_argument(
        "--mailbox-override", choices=["gmail", "outlook"], default=None,
        help="Force a provider other than the deployment's configured default, using that "
             "deployment's own secrets/ credentials. To stage against a throwaway inbox before "
             "a real customer mailbox, create a separate test deployment config instead — see README.",
    )
    run_p.add_argument(
        "--skip-billing-check", action="store_true",
        help="Skip the Stripe subscription check. For local dev/testing only — every scheduled "
             "cloud run must go through the real check, which is why this isn't the default.",
    )

    args = parser.parse_args()

    if args.resume_from and not args.run_id:
        parser.error("--resume-from requires --run-id (the run to resume)")

    try:
        asyncio.run(main_async(args))
    except SubscriptionInactive as exc:
        print(f"[outbound-agent] BILLING GATE: {exc}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
