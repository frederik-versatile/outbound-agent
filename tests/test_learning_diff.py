import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import poll_for_edits
from poll_for_edits import _compute_diff, poll_once
from store import LocalFileStore


def test_compute_diff_is_deterministic_and_shows_change():
    before = "Hi there,\nWe help teams launch outbound.\nThanks."
    after = "Hi Jordan,\nWe help revenue teams launch outbound fast.\nThanks."
    diff = _compute_diff(before, after)
    assert "-Hi there," in diff
    assert "+Hi Jordan," in diff
    assert _compute_diff(before, after) == diff  # deterministic


def test_compute_diff_empty_when_texts_equal():
    same = "Nothing changed here."
    assert _compute_diff(same, same) == ""


class _FakeMailbox:
    """Duck-types the subset of MailboxClient poll_for_edits.poll_once uses."""

    def __init__(self, finalized_text=None, current_draft_text=None):
        self._finalized_text = finalized_text
        self._current_draft_text = current_draft_text

    def get_finalized_text(self, tracking_id, draft_ref):
        return self._finalized_text

    def get_current_draft_text(self, draft_ref):
        return self._current_draft_text


class _FakeDeployment:
    def __init__(self, tmp_path, learning_kwargs=None):
        from deployment import LearningConfig

        self.deployment_id = "test"
        self.store = LocalFileStore(tmp_path)
        self.learning = LearningConfig(**(learning_kwargs or {}))
        self.models = {"learning": "claude-sonnet-5"}

    def state_key(self, *parts: str) -> str:
        return f"state/{self.deployment_id}/" + "/".join(parts)


def _write_audit(deployment, entries):
    deployment.store.write_text(deployment.state_key("drafts_audit.json"), json.dumps(entries))


def _read_audit(deployment):
    raw = deployment.store.read_text(deployment.state_key("drafts_audit.json"))
    return json.loads(raw)


def test_poll_once_detects_sent_draft_and_marks_learned(tmp_path):
    deployment = _FakeDeployment(tmp_path)
    _write_audit(deployment, [{
        "tracking_id": "t1", "draft_ref": "d1", "status": "open",
        "before_text": "Hi there,\nOriginal pitch.\nThanks.",
        "account_name": "Acme Robotics", "stakeholder_name": "Jordan Reyes",
    }])

    with patch.object(poll_for_edits, "MailboxClient", return_value=_FakeMailbox(
        finalized_text="Hi Jordan,\nEdited pitch.\nThanks."
    )), patch.object(poll_for_edits, "_invoke_learning_agent", new_callable=AsyncMock) as mock_learn:
        learned = asyncio.run(poll_once(deployment, dry_run=False))

    assert learned == 1
    audit = _read_audit(deployment)
    assert audit[0]["status"] == "learned_sent"
    mock_learn.assert_called_once()


def test_poll_once_skips_unchanged_sent_draft(tmp_path):
    deployment = _FakeDeployment(tmp_path)
    before = "Hi there,\nOriginal pitch.\nThanks."
    _write_audit(deployment, [{
        "tracking_id": "t1", "draft_ref": "d1", "status": "open",
        "before_text": before, "account_name": "Acme Robotics", "stakeholder_name": "Jordan Reyes",
    }])

    with patch.object(poll_for_edits, "MailboxClient", return_value=_FakeMailbox(finalized_text=before)):
        learned = asyncio.run(poll_once(deployment, dry_run=False))

    assert learned == 0
    audit = _read_audit(deployment)
    assert audit[0]["status"] == "unchanged_sent"


def test_poll_once_ignores_open_draft_when_stable_signal_disabled(tmp_path):
    deployment = _FakeDeployment(tmp_path, {"enable_stable_draft_signal": False})
    _write_audit(deployment, [{
        "tracking_id": "t1", "draft_ref": "d1", "status": "open",
        "before_text": "Hi there,\nOriginal pitch.\nThanks.",
        "account_name": "Acme Robotics", "stakeholder_name": "Jordan Reyes",
    }])

    with patch.object(poll_for_edits, "MailboxClient", return_value=_FakeMailbox(
        finalized_text=None, current_draft_text="Hi Jordan,\nEdited pitch.\nThanks."
    )):
        learned = asyncio.run(poll_once(deployment, dry_run=False))

    assert learned == 0
    audit = _read_audit(deployment)
    assert audit[0]["status"] == "open"  # untouched — half-finished edits shouldn't teach anything


def test_poll_once_waits_for_settle_window_before_treating_open_draft_as_finished(tmp_path):
    deployment = _FakeDeployment(tmp_path, {"enable_stable_draft_signal": True, "stable_draft_settle_hours": 6})
    _write_audit(deployment, [{
        "tracking_id": "t1", "draft_ref": "d1", "status": "open",
        "before_text": "Hi there,\nOriginal pitch.\nThanks.",
        "account_name": "Acme Robotics", "stakeholder_name": "Jordan Reyes",
    }])

    mailbox = _FakeMailbox(finalized_text=None, current_draft_text="Hi Jordan,\nEdited pitch.\nThanks.")

    with patch.object(poll_for_edits, "MailboxClient", return_value=mailbox):
        learned_first_poll = asyncio.run(poll_once(deployment, dry_run=False))

    audit = _read_audit(deployment)
    assert learned_first_poll == 0
    assert audit[0]["status"] == "open"
    assert "changed_first_seen_at" in audit[0]  # first sighting recorded, not yet learned from

    # Simulate the settle window having already elapsed, then poll again.
    audit[0]["changed_first_seen_at"] = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
    _write_audit(deployment, audit)

    with patch.object(poll_for_edits, "MailboxClient", return_value=mailbox), \
         patch.object(poll_for_edits, "_invoke_learning_agent", new_callable=AsyncMock) as mock_learn:
        learned_second_poll = asyncio.run(poll_once(deployment, dry_run=False))

    assert learned_second_poll == 1
    audit = _read_audit(deployment)
    assert audit[0]["status"] == "learned_stable_unsent"
