from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import sequencing
from deployment import SequenceConfig, SequenceStep
from store import LocalFileStore


@dataclass
class _FakeDeployment:
    deployment_id: str
    store: LocalFileStore
    sequence: SequenceConfig

    def state_key(self, *parts: str) -> str:
        return f"state/{self.deployment_id}/" + "/".join(parts)


def _deployment(tmp_path, wait_days=(4, 4, 4), recycle_after_days=90):
    steps = [SequenceStep(name=n, wait_days=w, angle=f"{n} angle") for n, w in zip(("opener", "bump", "breakup"), wait_days)]
    return _FakeDeployment(
        deployment_id="test",
        store=LocalFileStore(tmp_path),
        sequence=SequenceConfig(steps=steps, recycle_after_days=recycle_after_days),
    )


def _draft(**overrides):
    base = {
        "to": "Jordan.Reyes@Acme.com", "tracking_id": "tr-1", "thread_id": "th-1",
        "account_name": "Acme Robotics", "stakeholder_name": "Jordan Reyes", "subject": "Quick question",
    }
    base.update(overrides)
    return base


def test_seed_step_zero_creates_entry(tmp_path):
    d = _deployment(tmp_path)
    sequencing.seed_step_zero(d, [_draft()])
    entries = sequencing.load_sequences(d)
    assert len(entries) == 1
    assert entries[0]["lead_key"] == "jordan.reyes@acme.com"  # lowercased
    assert entries[0]["step"] == 0
    assert entries[0]["status"] == "drafted"


def test_is_suppressed_case_insensitive(tmp_path):
    d = _deployment(tmp_path)
    sequencing.seed_step_zero(d, [_draft()])
    entries = sequencing.load_sequences(d)
    assert sequencing.is_suppressed(entries, "JORDAN.REYES@ACME.COM")
    assert not sequencing.is_suppressed(entries, "someone.else@acme.com")


def test_mark_sent_transitions_to_awaiting_reply_with_correct_wait(tmp_path):
    d = _deployment(tmp_path, wait_days=(4, 4, 4))
    sequencing.seed_step_zero(d, [_draft()])

    matched = sequencing.mark_sent(d, "tr-1")
    assert matched is True

    entry = sequencing.load_sequences(d)[0]
    assert entry["status"] == "awaiting_reply"
    assert entry["sent_at"] is not None
    expected = datetime.fromisoformat(entry["sent_at"]) + timedelta(days=4)
    assert datetime.fromisoformat(entry["reply_check_at"]) == expected


def test_mark_sent_no_match_returns_false(tmp_path):
    d = _deployment(tmp_path)
    sequencing.seed_step_zero(d, [_draft()])
    assert sequencing.mark_sent(d, "not-a-real-tracking-id") is False


def test_mark_sent_is_idempotent_only_transitions_drafted_entries(tmp_path):
    d = _deployment(tmp_path)
    sequencing.seed_step_zero(d, [_draft()])
    sequencing.mark_sent(d, "tr-1")
    first_reply_check_at = sequencing.load_sequences(d)[0]["reply_check_at"]

    # Calling again shouldn't re-fire (entry is no longer "drafted")
    matched_again = sequencing.mark_sent(d, "tr-1")
    assert matched_again is False
    assert sequencing.load_sequences(d)[0]["reply_check_at"] == first_reply_check_at


def test_due_for_reply_check_respects_timer(tmp_path):
    d = _deployment(tmp_path)
    sequencing.seed_step_zero(d, [_draft()])
    sequencing.mark_sent(d, "tr-1")

    assert sequencing.due_for_reply_check(d) == []  # timer hasn't elapsed yet

    entries = sequencing.load_sequences(d)
    entries[0]["reply_check_at"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    sequencing.save_sequences(d, entries)

    due = sequencing.due_for_reply_check(d)
    assert len(due) == 1
    assert due[0]["lead_key"] == "jordan.reyes@acme.com"


def test_mark_replied_is_terminal(tmp_path):
    d = _deployment(tmp_path)
    sequencing.seed_step_zero(d, [_draft()])
    sequencing.mark_sent(d, "tr-1")
    sequencing.mark_replied(d, "jordan.reyes@acme.com")

    entry = sequencing.load_sequences(d)[0]
    assert entry["status"] == "replied"
    assert sequencing.is_suppressed([entry], "jordan.reyes@acme.com")  # still suppressed — don't re-contact


def test_advance_to_next_step_resets_timers_and_keeps_lead_suppressed(tmp_path):
    d = _deployment(tmp_path)
    sequencing.seed_step_zero(d, [_draft()])
    sequencing.mark_sent(d, "tr-1")

    sequencing.advance_to_next_step(d, "jordan.reyes@acme.com", 1, "tr-2", "th-1", "Re: Quick question")

    entry = sequencing.load_sequences(d)[0]
    assert entry["step"] == 1
    assert entry["step_tracking_id"] == "tr-2"
    assert entry["status"] == "drafted"
    assert entry["sent_at"] is None
    assert entry["reply_check_at"] is None
    assert sequencing.is_suppressed([entry], "jordan.reyes@acme.com")


def test_full_lifecycle_to_recycle_and_back_to_eligible(tmp_path):
    d = _deployment(tmp_path, recycle_after_days=90)
    sequencing.seed_step_zero(d, [_draft()])
    sequencing.mark_sent(d, "tr-1")

    sequencing.start_recycle_clock(d, "jordan.reyes@acme.com")
    entry = sequencing.load_sequences(d)[0]
    assert entry["status"] == "recycling"
    assert sequencing.is_suppressed([entry], "jordan.reyes@acme.com")
    assert sequencing.due_for_recycle(d) == []  # 90 days out, not due yet

    entries = sequencing.load_sequences(d)
    entries[0]["recycle_at"] = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    sequencing.save_sequences(d, entries)
    assert len(sequencing.due_for_recycle(d)) == 1

    sequencing.mark_eligible(d, "jordan.reyes@acme.com")
    entry = sequencing.load_sequences(d)[0]
    assert entry["status"] == "eligible"
    assert not sequencing.is_suppressed([entry], "jordan.reyes@acme.com")
