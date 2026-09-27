import asyncio
from unittest.mock import AsyncMock, patch

import advance_sequences
import sequencing
from deployment import SequenceConfig, SequenceStep
from store import LocalFileStore


class _FakeDeployment:
    def __init__(self, tmp_path, wait_days=(4, 4, 4)):
        steps = [
            SequenceStep(name=n, wait_days=w, angle=f"{n} angle")
            for n, w in zip(("opener", "bump", "breakup"), wait_days)
        ]
        self.deployment_id = "test"
        self.store = LocalFileStore(tmp_path)
        self.sequence = SequenceConfig(steps=steps, recycle_after_days=90)
        self.models = {"drafting": "claude-sonnet-5"}

    def state_key(self, *parts: str) -> str:
        return f"state/{self.deployment_id}/" + "/".join(parts)


class _FakeMailbox:
    def __init__(self, has_reply: bool):
        self._has_reply = has_reply

    def check_reply(self, thread_id, recipient_email):
        return {"has_reply": self._has_reply, "last_message_id_header": "<msg-1@example.com>"}


def _seed_due_entry(deployment, step=0):
    sequencing.seed_step_zero(deployment, [{
        "to": "jordan.reyes@acme.com", "tracking_id": "tr-1", "thread_id": "th-1",
        "account_name": "Acme Robotics", "stakeholder_name": "Jordan Reyes", "subject": "Quick question",
    }])
    sequencing.mark_sent(deployment, "tr-1")
    if step > 0:
        sequencing.advance_to_next_step(deployment, "jordan.reyes@acme.com", step, f"tr-{step+1}", "th-1", "Re: Quick question")
        sequencing.mark_sent(deployment, f"tr-{step+1}")
    entries = sequencing.load_sequences(deployment)
    entries[0]["reply_check_at"] = "2000-01-01T00:00:00+00:00"  # force it into the past
    sequencing.save_sequences(deployment, entries)


def test_replied_lead_is_marked_and_no_draft_attempted(tmp_path):
    d = _FakeDeployment(tmp_path)
    _seed_due_entry(d)

    with patch.object(advance_sequences, "MailboxClient", return_value=_FakeMailbox(has_reply=True)), \
         patch.object(advance_sequences, "_draft_next_step", new_callable=AsyncMock) as mock_draft:
        stats = asyncio.run(advance_sequences.run_once(d, dry_run=False))

    assert stats["replied"] == 1
    mock_draft.assert_not_called()
    assert sequencing.load_sequences(d)[0]["status"] == "replied"


def test_no_reply_with_next_step_available_advances(tmp_path):
    d = _FakeDeployment(tmp_path)
    _seed_due_entry(d, step=0)

    with patch.object(advance_sequences, "MailboxClient", return_value=_FakeMailbox(has_reply=False)), \
         patch.object(advance_sequences, "_draft_next_step", new_callable=AsyncMock) as mock_draft:
        mock_draft.return_value = {"tracking_id": "tr-2", "thread_id": "th-1", "subject": "Re: Quick question"}
        stats = asyncio.run(advance_sequences.run_once(d, dry_run=False))

    assert stats["advanced"] == 1
    mock_draft.assert_called_once()
    entry = sequencing.load_sequences(d)[0]
    assert entry["step"] == 1
    assert entry["step_tracking_id"] == "tr-2"
    assert entry["status"] == "drafted"


def test_no_reply_on_last_step_starts_recycle_clock(tmp_path):
    d = _FakeDeployment(tmp_path)
    _seed_due_entry(d, step=2)  # step index 2 = "breakup", the last configured step

    with patch.object(advance_sequences, "MailboxClient", return_value=_FakeMailbox(has_reply=False)), \
         patch.object(advance_sequences, "_draft_next_step", new_callable=AsyncMock) as mock_draft:
        stats = asyncio.run(advance_sequences.run_once(d, dry_run=False))

    assert stats["recycling_started"] == 1
    mock_draft.assert_not_called()
    entry = sequencing.load_sequences(d)[0]
    assert entry["status"] == "recycling"
    assert entry["recycle_at"] is not None


def test_failed_draft_leaves_entry_untouched_for_retry(tmp_path):
    d = _FakeDeployment(tmp_path)
    _seed_due_entry(d, step=0)
    before = sequencing.load_sequences(d)[0]

    with patch.object(advance_sequences, "MailboxClient", return_value=_FakeMailbox(has_reply=False)), \
         patch.object(advance_sequences, "_draft_next_step", new_callable=AsyncMock) as mock_draft:
        mock_draft.return_value = None  # simulates the agent failing to produce a draft
        stats = asyncio.run(advance_sequences.run_once(d, dry_run=False))

    assert stats["no_draft_produced"] == 1
    after = sequencing.load_sequences(d)[0]
    assert after == before  # untouched, so the next run retries it


def test_dry_run_checks_but_does_not_mutate_state(tmp_path):
    d = _FakeDeployment(tmp_path)
    _seed_due_entry(d)
    before = sequencing.load_sequences(d)

    with patch.object(advance_sequences, "MailboxClient", return_value=_FakeMailbox(has_reply=True)), \
         patch.object(advance_sequences, "_draft_next_step", new_callable=AsyncMock) as mock_draft:
        stats = asyncio.run(advance_sequences.run_once(d, dry_run=True))

    assert stats["replied"] == 1  # the check still happened and was counted
    mock_draft.assert_not_called()
    assert sequencing.load_sequences(d) == before  # but nothing was written


def test_recycle_eligible_leads_are_flipped(tmp_path):
    d = _FakeDeployment(tmp_path)
    sequencing.seed_step_zero(d, [{
        "to": "jordan.reyes@acme.com", "tracking_id": "tr-1", "thread_id": "th-1",
        "account_name": "Acme Robotics", "stakeholder_name": "Jordan Reyes", "subject": "Quick question",
    }])
    sequencing.mark_sent(d, "tr-1")
    sequencing.start_recycle_clock(d, "jordan.reyes@acme.com")
    entries = sequencing.load_sequences(d)
    entries[0]["recycle_at"] = "2000-01-01T00:00:00+00:00"
    sequencing.save_sequences(d, entries)

    with patch.object(advance_sequences, "MailboxClient", return_value=_FakeMailbox(has_reply=False)):
        stats = asyncio.run(advance_sequences.run_once(d, dry_run=False))

    assert stats["recycled_eligible"] == 1
    assert sequencing.load_sequences(d)[0]["status"] == "eligible"
