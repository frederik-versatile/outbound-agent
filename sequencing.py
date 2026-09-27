"""Shared read/write + state-transition helpers for the per-lead sequence
tracker (state/<id>/sequences.json). One entry per lead, keyed by lead_key
(the recipient's email, lowercased) — a "lead" is a specific person at a
specific account, not the account itself.

Used by:
- orchestrator.py: seeds a step-0 entry per newly drafted cold opener, and
  checks is_suppressed() before drafting so the same lead isn't re-contacted
  daily while already mid-sequence, already replied, or still inside the
  recycle cooldown.
- poll_for_edits.py: once it confirms a tracked draft was actually sent
  (it already does this detection for the learning loop), flips that lead's
  entry from "drafted" to "awaiting_reply" and starts the reply-check timer.
- advance_sequences.py: checks leads whose reply-check timer has elapsed,
  advances them to the next step or starts the recycle clock.

Status lifecycle: drafted -> awaiting_reply -> replied (terminal)
                                             -> [next step] -> drafted -> ...
                                             -> recycling -> eligible (no
                                                longer suppressed; not a
                                                terminal state, may re-enter
                                                at step 0 on a future run)
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from deployment import Deployment

ACTIVE_STATUSES = {"drafted", "awaiting_reply", "replied", "recycling"}


def _key(deployment: Deployment) -> str:
    return deployment.state_key("sequences.json")


def load_sequences(deployment: Deployment) -> list[dict[str, Any]]:
    raw = deployment.store.read_text(_key(deployment))
    return json.loads(raw) if raw else []


def save_sequences(deployment: Deployment, entries: list[dict[str, Any]]) -> None:
    deployment.store.write_text(_key(deployment), json.dumps(entries, indent=2))


def is_suppressed(entries: list[dict[str, Any]], email: str) -> bool:
    """True if this lead is currently mid-sequence, already replied, or
    still inside the recycle cooldown — i.e. should NOT be drafted a fresh
    cold opener right now."""
    email = email.lower()
    return any(e["lead_key"] == email and e["status"] in ACTIVE_STATUSES for e in entries)


def seed_step_zero(deployment: Deployment, drafts_created: list[dict[str, Any]]) -> None:
    """Called by orchestrator.py right after the drafting stage creates this
    run's cold-opener drafts. One new sequence entry per lead."""
    entries = load_sequences(deployment)
    for draft in drafts_created:
        entries.append({
            "lead_key": draft["to"].lower(),
            "account_name": draft["account_name"],
            "stakeholder_name": draft["stakeholder_name"],
            "thread_id": draft["thread_id"],
            "subject": draft.get("subject"),
            "step": 0,
            "step_tracking_id": draft["tracking_id"],
            "status": "drafted",
            "sent_at": None,
            "reply_check_at": None,
            "recycle_at": None,
        })
    save_sequences(deployment, entries)


def mark_sent(deployment: Deployment, tracking_id: str) -> bool:
    """Called by poll_for_edits.py once it confirms the draft with this
    tracking_id was actually sent. Returns True if a sequence entry matched
    (poll_for_edits.py only cares about the result for sequence bookkeeping;
    plenty of sent drafts have no sequence entry at all, which is fine)."""
    entries = load_sequences(deployment)
    matched = False
    now = datetime.now(timezone.utc)
    for entry in entries:
        if entry["step_tracking_id"] == tracking_id and entry["status"] == "drafted":
            wait_days = deployment.sequence.steps[entry["step"]].wait_days
            entry["status"] = "awaiting_reply"
            entry["sent_at"] = now.isoformat()
            entry["reply_check_at"] = (now + timedelta(days=wait_days)).isoformat()
            matched = True
    if matched:
        save_sequences(deployment, entries)
    return matched


def due_for_reply_check(deployment: Deployment) -> list[dict[str, Any]]:
    entries = load_sequences(deployment)
    now = datetime.now(timezone.utc)
    return [
        e for e in entries
        if e["status"] == "awaiting_reply" and e["reply_check_at"] and datetime.fromisoformat(e["reply_check_at"]) <= now
    ]


def due_for_recycle(deployment: Deployment) -> list[dict[str, Any]]:
    entries = load_sequences(deployment)
    now = datetime.now(timezone.utc)
    return [
        e for e in entries
        if e["status"] == "recycling" and e["recycle_at"] and datetime.fromisoformat(e["recycle_at"]) <= now
    ]


def mark_replied(deployment: Deployment, lead_key: str) -> None:
    entries = load_sequences(deployment)
    for entry in entries:
        if entry["lead_key"] == lead_key.lower():
            entry["status"] = "replied"
    save_sequences(deployment, entries)


def advance_to_next_step(
    deployment: Deployment, lead_key: str, new_step: int, new_tracking_id: str, new_thread_id: str, subject: str
) -> None:
    entries = load_sequences(deployment)
    for entry in entries:
        if entry["lead_key"] == lead_key.lower():
            entry["step"] = new_step
            entry["step_tracking_id"] = new_tracking_id
            entry["thread_id"] = new_thread_id
            entry["subject"] = subject
            entry["status"] = "drafted"
            entry["sent_at"] = None
            entry["reply_check_at"] = None
    save_sequences(deployment, entries)


def start_recycle_clock(deployment: Deployment, lead_key: str) -> None:
    entries = load_sequences(deployment)
    now = datetime.now(timezone.utc)
    for entry in entries:
        if entry["lead_key"] == lead_key.lower():
            entry["status"] = "recycling"
            entry["recycle_at"] = (now + timedelta(days=deployment.sequence.recycle_after_days)).isoformat()
    save_sequences(deployment, entries)


def mark_eligible(deployment: Deployment, lead_key: str) -> None:
    entries = load_sequences(deployment)
    for entry in entries:
        if entry["lead_key"] == lead_key.lower():
            entry["status"] = "eligible"
            entry["recycle_at"] = None
    save_sequences(deployment, entries)
