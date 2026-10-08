import asyncio
import json
from unittest.mock import MagicMock

from agents.style_rules import EM_DASH
from tools.mailbox_tools import build_mailbox_tools, em_dash_error
from tools.preview_mailbox_tools import build_preview_mailbox_tools


def test_em_dash_error_none_for_clean_text():
    assert em_dash_error("A clean subject", "A clean body, with commas.") is None


def test_em_dash_error_detects_in_subject():
    assert em_dash_error(f"Subject with{EM_DASH}dash", "clean body") is not None


def test_em_dash_error_detects_in_body():
    assert em_dash_error("clean subject", f"body with an{EM_DASH}dash in it") is not None


def _args(**overrides):
    base = {
        "stakeholder_id": "p1", "subject": "Clean subject", "body_text": "Hi {{first_name}}, clean body.",
        "account_name": "Acme Robotics",
    }
    base.update(overrides)
    return base


class _FakeDeployment:
    def __init__(self, tmp_path):
        from store import LocalFileStore
        self.deployment_id = "test"
        self.store = LocalFileStore(tmp_path)

    def state_key(self, *parts):
        return f"state/{self.deployment_id}/" + "/".join(parts)


def _vault(tmp_path):
    from contacts import ContactVault
    vault = ContactVault(_FakeDeployment(tmp_path))
    vault.remember({"id": "p1", "first_name": "Jordan", "last_name": "Reyes",
                    "email": "jordan.reyes@acme.example.com", "email_status": "verified", "title": "VP Sales"})
    return vault


def test_live_mailbox_tool_rejects_em_dash_and_never_calls_mailbox(tmp_path):
    deployment = _FakeDeployment(tmp_path)
    fake_mailbox = MagicMock()
    tools = build_mailbox_tools(fake_mailbox, deployment, max_drafts_per_run=5, run_dir=tmp_path,
                                vault=_vault(tmp_path))
    create_draft = tools[0]

    result = asyncio.run(create_draft.handler(_args(body_text=f"Great fit{EM_DASH}let's talk.")))

    assert result.get("is_error") is True
    fake_mailbox.create_draft.assert_not_called()
    audit_raw = deployment.store.read_text(deployment.state_key("drafts_audit.json"))
    assert audit_raw is None  # nothing was ever written


def test_live_mailbox_tool_accepts_clean_copy(tmp_path):
    deployment = _FakeDeployment(tmp_path)
    fake_mailbox = MagicMock()
    fake_mailbox.create_draft.return_value = {"draft_ref": "d1", "provider": "gmail", "thread_id": "t1"}
    tools = build_mailbox_tools(fake_mailbox, deployment, max_drafts_per_run=5, run_dir=tmp_path,
                                vault=_vault(tmp_path))
    create_draft = tools[0]

    result = asyncio.run(create_draft.handler(_args()))

    assert not result.get("is_error")
    fake_mailbox.create_draft.assert_called_once()


def test_preview_mailbox_tool_rejects_em_dash(tmp_path):
    tools = build_preview_mailbox_tools(tmp_path, max_drafts_per_run=5, vault=_vault(tmp_path))
    create_draft = tools[0]

    result = asyncio.run(create_draft.handler(_args(subject=f"Quick question{EM_DASH}got a sec?")))

    assert result.get("is_error") is True
    assert not (tmp_path / "drafts_created.json").exists()
    assert list((tmp_path / "drafts_preview").glob("*.md")) == []


def test_preview_mailbox_tool_accepts_clean_copy(tmp_path):
    tools = build_preview_mailbox_tools(tmp_path, max_drafts_per_run=5, vault=_vault(tmp_path))
    create_draft = tools[0]

    result = asyncio.run(create_draft.handler(_args()))

    assert not result.get("is_error")
    assert len(list((tmp_path / "drafts_preview").glob("*.md"))) == 1
