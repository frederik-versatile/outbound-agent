"""Resolves a --deployment <id> into its config, durable store, and env vars.

Where a deployment lives (client-folder model, see Agents/Hub/Locks.md): each customer is a client
project, and its outbound config sits in that client's folder:

    Agents/Clients/<Client>/outbound/outbound.yaml   deployment config (deployment_id inside)
    Agents/Clients/<Client>/outbound/icp.md           ICP criteria
    Agents/Clients/<Client>/outbound/positioning.md   positioning / value prop
    Agents/Clients/<Client>/outbound/sequence.yaml    cadence

OUTBOUND_CLIENTS_DIR overrides where client folders are looked up (e.g. a bundled copy in a cloud
deploy). config/ in this repo only holds the _example templates. Durable state and secrets stay in
this repo's state/<deployment_id>/ and secrets/<deployment_id>/ (code-only: they hold the contact
store and mailbox tokens, which no Claude session may read).

Every entrypoint (orchestrator.py, poll_for_edits.py, the setup_*.py scripts)
goes through this module so that onboarding a new customer is "add a YAML
file + run the setup scripts + two .env lines," never a code change.

Durable cross-invocation state (OAuth tokens, drafts_audit.json,
style_notes.md, run_log.jsonl) goes through deployment.store (see
store.py) rather than local files, because in production (Render Cron
Jobs) the filesystem resets on every run. Per-run scratch data that only
needs to survive within one process invocation (accounts_scored.json,
stakeholders.json, drafts_preview/*.md) still uses plain local disk via
run_dir() — see store.py's module docstring for why that split is safe.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

from store import Store, build_store

ROOT = Path(__file__).parent
CONFIG_DIR = ROOT / "config"  # _example templates only
STATE_DIR = ROOT / "state"
CLIENTS_DIR = Path(os.environ.get("OUTBOUND_CLIENTS_DIR") or ROOT.parents[2] / "Clients")

load_dotenv(ROOT / ".env")


@dataclass
class SafetyLimits:
    dry_run_default: bool = True
    max_accounts_per_run: int = 25
    max_contacts_per_account: int = 2
    max_total_enrich_calls_per_run: int = 50
    max_drafts_per_run: int = 25


@dataclass
class LearningConfig:
    enable_stable_draft_signal: bool = False
    stable_draft_settle_hours: int = 6
    style_notes_token_budget: int = 4000


@dataclass
class SequenceStep:
    name: str
    wait_days: int
    angle: str


@dataclass
class SequenceConfig:
    steps: list[SequenceStep]
    recycle_after_days: int = 90


@dataclass
class HireSheetConfig:
    """Optional cross-reference against a customer-tracked buying-signal
    sheet — never the primary source of accounts, only a score boost/
    annotation for Apollo-sourced accounts that also appear on it. See
    orchestrator.py's cross_reference_hire_sheet()."""
    enabled: bool = False
    spreadsheet_id: str = ""
    range: str = "Sheet1!A2:B"
    name_column: int = 0
    domain_column: int = 1
    score_boost: int = 15


@dataclass
class Deployment:
    deployment_id: str
    mail_provider: str
    apollo_api_key_env: str
    icp_doc: Path
    positioning_doc: Path
    target_titles: list[str]
    target_seniorities: list[str]
    models: dict[str, str]
    safety: SafetyLimits
    learning: LearningConfig
    sequence: SequenceConfig
    hire_sheet: HireSheetConfig
    store: Store
    outlook_client_id: str = ""
    outlook_tenant_id: str = ""
    stripe_subscription_id: str = ""
    client: str = ""
    client_dir: Path | None = None
    sender_names: list[str] = field(default_factory=list)

    def secret_key(self, filename: str) -> str:
        """Storage key for a durable secret (OAuth tokens, client secret)."""
        return f"secrets/{self.deployment_id}/{filename}"

    def state_key(self, *parts: str) -> str:
        """Storage key for durable cross-invocation state (audit trail,
        style notes, run log) — NOT for per-run scratch data, see run_dir()."""
        return f"state/{self.deployment_id}/" + "/".join(parts)

    def run_dir(self, run_id: str) -> Path:
        """Local ephemeral directory for one pipeline run's stage-to-stage
        handoff files. Safe to use plain disk here even in production,
        because orchestrator.py's stages all run within one process
        invocation — nothing here needs to survive to a later invocation."""
        d = STATE_DIR / self.deployment_id / "runs" / run_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def keychain_service(self) -> str:
        return f"refactor-outbound-{self.client or self.deployment_id}"

    @property
    def apollo_api_key(self) -> str:
        """This client's own Apollo key: macOS Keychain first (local, per client), then the env var
        named in outbound.yaml (cloud deploys, where there is no Keychain)."""
        try:
            r = subprocess.run(["security", "find-generic-password", "-s", self.keychain_service,
                                "-a", "apollo", "-w"], capture_output=True, text=True)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
        except FileNotFoundError:
            pass  # not macOS
        key = os.environ.get(self.apollo_api_key_env) if self.apollo_api_key_env else None
        if not key:
            raise RuntimeError(
                f"No Apollo key for {self.client or self.deployment_id}: run "
                f"setup_apollo.py --deployment {self.deployment_id} (stores it in the Keychain)."
            )
        return key

    def read_icp_doc(self) -> str:
        return self.icp_doc.read_text()

    def read_positioning_doc(self) -> str:
        return self.positioning_doc.read_text()


def find_client_outbound_dir(deployment_id: str) -> tuple[str, Path]:
    """(client name, Clients/<Client>/outbound) for the client whose outbound.yaml declares this
    deployment_id. Exactly one client may claim a deployment_id."""
    matches = []
    for cfg in sorted(CLIENTS_DIR.glob("*/outbound/outbound.yaml")):
        raw = yaml.safe_load(cfg.read_text()) or {}
        if raw.get("deployment_id") == deployment_id:
            matches.append((cfg.parent.parent.name, cfg.parent))
    if not matches:
        raise FileNotFoundError(
            f"No client folder declares outbound deployment '{deployment_id}'. Create "
            f"{CLIENTS_DIR}/<Client>/outbound/outbound.yaml from config/deployments/_example.yaml."
        )
    if len(matches) > 1:
        raise ValueError(f"Deployment '{deployment_id}' is claimed by several clients: "
                         + ", ".join(m[0] for m in matches))
    return matches[0]


def load_deployment(deployment_id: str) -> Deployment:
    client, outbound_dir = find_client_outbound_dir(deployment_id)
    config_path = outbound_dir / "outbound.yaml"
    raw = yaml.safe_load(config_path.read_text())

    if raw.get("deployment_id") != deployment_id:
        raise ValueError(
            f"{config_path} declares deployment_id={raw.get('deployment_id')!r}, "
            f"expected {deployment_id!r} (filename must match deployment_id)."
        )

    safety_raw = raw.get("safety", {})
    learning_raw = raw.get("learning", {})
    outlook_raw = raw.get("outlook", {})
    billing_raw = raw.get("billing", {})
    hire_sheet_raw = raw.get("hire_sheet", {})
    sequence = _load_sequence(outbound_dir)

    return Deployment(
        deployment_id=deployment_id,
        mail_provider=raw["mail_provider"],
        apollo_api_key_env=raw.get("apollo_api_key_env", ""),
        icp_doc=outbound_dir / raw.get("icp_doc", "icp.md"),
        positioning_doc=outbound_dir / raw.get("positioning_doc", "positioning.md"),
        target_titles=raw.get("target_titles", []),
        target_seniorities=raw.get("target_seniorities", []),
        models=raw.get("models", {}),
        safety=SafetyLimits(
            dry_run_default=safety_raw.get("dry_run_default", True),
            max_accounts_per_run=safety_raw.get("max_accounts_per_run", 25),
            max_contacts_per_account=safety_raw.get("max_contacts_per_account", 2),
            max_total_enrich_calls_per_run=safety_raw.get("max_total_enrich_calls_per_run", 50),
            max_drafts_per_run=safety_raw.get("max_drafts_per_run", 25),
        ),
        learning=LearningConfig(
            enable_stable_draft_signal=learning_raw.get("enable_stable_draft_signal", False),
            stable_draft_settle_hours=learning_raw.get("stable_draft_settle_hours", 6),
            style_notes_token_budget=learning_raw.get("style_notes_token_budget", 4000),
        ),
        sequence=sequence,
        hire_sheet=HireSheetConfig(
            enabled=hire_sheet_raw.get("enabled", False),
            spreadsheet_id=hire_sheet_raw.get("spreadsheet_id", ""),
            range=hire_sheet_raw.get("range", "Sheet1!A2:B"),
            name_column=hire_sheet_raw.get("name_column", 0),
            domain_column=hire_sheet_raw.get("domain_column", 1),
            score_boost=hire_sheet_raw.get("score_boost", 15),
        ),
        store=build_store(ROOT),
        outlook_client_id=outlook_raw.get("client_id", ""),
        outlook_tenant_id=outlook_raw.get("tenant_id", ""),
        stripe_subscription_id=billing_raw.get("stripe_subscription_id", ""),
        client=client,
        client_dir=outbound_dir.parent,
        sender_names=list(raw.get("sender_names", [])),
    )


def _load_sequence(outbound_dir: Path) -> SequenceConfig:
    path = outbound_dir / "sequence.yaml"
    if not path.exists():
        raise FileNotFoundError(
            f"No sequence config at {path}. Copy config/sequences/_example.yaml to get started."
        )
    raw = yaml.safe_load(path.read_text())
    steps = [
        SequenceStep(name=s["name"], wait_days=s["wait_days"], angle=s["angle"].strip())
        for s in raw["steps"]
    ]
    return SequenceConfig(steps=steps, recycle_after_days=raw.get("recycle_after_days", 90))


def list_deployments() -> list[str]:
    ids = []
    for cfg in CLIENTS_DIR.glob("*/outbound/outbound.yaml"):
        raw = yaml.safe_load(cfg.read_text()) or {}
        if raw.get("deployment_id"):
            ids.append(raw["deployment_id"])
    return sorted(ids)
