"""No-PII rule: no person's name or email address ever reaches an agent (Claude), at any stage.

The fake person below is the canary — every test asserts that her name and address never appear in
anything an agent receives (tool results, prompts), while the code-side mailbox still gets them.
"""

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from contacts import ContactVault, PlaceholderError
from deployment import LearningConfig, SequenceConfig, SequenceStep
from store import LocalFileStore

NAME, FIRST, LAST, EMAIL = "Jordan Reyes", "Jordan", "Reyes", "jordan.reyes@acme.example.com"


def assert_no_pii(text: str) -> None:
    for secret in (FIRST, LAST, EMAIL, "jordan.reyes"):
        assert secret.lower() not in text.lower(), f"PII leaked to the agent: {secret!r} in {text[:300]!r}"


class _FakeDeployment:
    def __init__(self, tmp_path):
        self.deployment_id = "test"
        self.store = LocalFileStore(tmp_path)
        self.sender_names = ["Frederik"]
        self.learning = LearningConfig()
        self.models = {"learning": "m", "drafting": "m"}
        self.sequence = SequenceConfig(steps=[SequenceStep("opener", 4, "a"), SequenceStep("bump", 4, "b")])

    def state_key(self, *parts):
        return f"state/{self.deployment_id}/" + "/".join(parts)

    def run_dir(self, run_id):
        d = self.store.root / "runs" / run_id
        d.mkdir(parents=True, exist_ok=True)
        return d


def _person():
    return {"id": "p1", "first_name": FIRST, "last_name": LAST, "name": NAME, "title": "VP Sales",
            "seniority": "vp", "organization_id": "o1", "email": EMAIL, "email_status": "verified"}


def _tool_text(result) -> str:
    return "".join(c.get("text", "") for c in result["content"])


# ---- Apollo tools -------------------------------------------------------------------------------

def test_apollo_people_tools_hide_names_and_emails_but_vault_keeps_them(tmp_path):
    from tools.apollo_tools import build_apollo_tools

    deployment = _FakeDeployment(tmp_path)
    vault = ContactVault(deployment)
    client = MagicMock()
    client.search_people.return_value = {"people": [{k: v for k, v in _person().items() if k != "email"}]}
    client.bulk_enrich_people.return_value = {"matches": [_person()]}
    client.enrich_person.return_value = {"person": _person()}
    tools = {t.name: t for t in build_apollo_tools(client, vault)}

    search = _tool_text(asyncio.run(tools["apollo_search_people"].handler({"organization_ids": ["o1"]})))
    bulk = _tool_text(asyncio.run(tools["apollo_bulk_enrich_people"].handler({"person_ids": ["p1"]})))
    one = _tool_text(asyncio.run(tools["apollo_enrich_person"].handler({"person_id": "p1"})))

    for text in (search, bulk, one):
        assert_no_pii(text)
    assert '"person_id": "p1"' in search and "VP Sales" in search
    assert '"has_email": true' in bulk
    client.bulk_enrich_people.assert_called_once_with([{"id": "p1"}])
    assert vault.email("p1") == EMAIL and vault.get("p1")["first_name"] == FIRST


def test_write_stakeholders_strips_personal_fields_and_drops_unverified(tmp_path):
    from tools.fs_tools import build_discovery_fs_tools

    deployment = _FakeDeployment(tmp_path)
    vault = ContactVault(deployment)
    vault.remember(_person())
    tools = {t.name: t for t in build_discovery_fs_tools(deployment, tmp_path, vault)}
    asyncio.run(tools["write_stakeholders"].handler({"stakeholders": [
        {"person_id": "p1", "name": NAME, "email": EMAIL, "title": "VP Sales", "account_name": "Acme"},
        {"person_id": "p2", "title": "CRO", "account_name": "Acme"},  # no verified email held -> dropped
    ]}))
    written = (tmp_path / "stakeholders.json").read_text()
    assert_no_pii(written)
    assert [s["person_id"] for s in json.loads(written)] == ["p1"]


# ---- Drafting -----------------------------------------------------------------------------------

def test_create_draft_merges_placeholders_code_side_and_keeps_template_in_audit(tmp_path):
    from tools.mailbox_tools import build_mailbox_tools

    deployment = _FakeDeployment(tmp_path)
    vault = ContactVault(deployment)
    vault.remember(_person())
    mailbox = MagicMock()
    mailbox.create_draft.return_value = {"draft_ref": "d1", "provider": "gmail", "thread_id": "t1"}
    tool = build_mailbox_tools(mailbox, deployment, 5, tmp_path, vault=vault)[0]

    result = asyncio.run(tool.handler({"stakeholder_id": "p1", "subject": "Idea for {{first_name}}",
                                       "body_text": "Hi {{first_name}},\nShort note.", "account_name": "Acme"}))

    assert not result.get("is_error")
    assert_no_pii(_tool_text(result))
    sent = mailbox.create_draft.call_args.kwargs
    assert sent["to"] == EMAIL and sent["body_text"].startswith(f"Hi {FIRST},") and FIRST in sent["subject"]
    audit = json.loads(deployment.store.read_text(deployment.state_key("drafts_audit.json")))[0]
    assert audit["before_text"] == "Hi {{first_name}},\nShort note."  # placeholder version only


@pytest.mark.parametrize("body,error", [
    (f"Hi, reply to {EMAIL}", "email address"),
    ("Hi {{nickname}}", "Unknown placeholder"),
])
def test_create_draft_rejects_addresses_and_unknown_placeholders(tmp_path, body, error):
    from tools.mailbox_tools import build_mailbox_tools

    deployment = _FakeDeployment(tmp_path)
    vault = ContactVault(deployment)
    vault.remember(_person())
    mailbox = MagicMock()
    tool = build_mailbox_tools(mailbox, deployment, 5, tmp_path, vault=vault)[0]
    result = asyncio.run(tool.handler({"stakeholder_id": "p1", "subject": "s", "body_text": body,
                                       "account_name": "Acme"}))
    assert result["is_error"] and error in _tool_text(result)
    mailbox.create_draft.assert_not_called()


def test_check_template_and_depersonalize_roundtrip():
    with pytest.raises(PlaceholderError):
        ContactVault.check_template(f"mail {EMAIL}")
    contact = _person()
    merged = ContactVault.merge("Hi {{first_name}}, regards Frederik", contact)
    assert merged == "Hi Jordan, regards Frederik"
    back = ContactVault.depersonalize(merged + f" ({EMAIL})", contact, ["Frederik"])
    assert back == "Hi {{first_name}}, regards {{sender_name}} ({{email}})"


# ---- Follow-ups and learning ------------------------------------------------------------------

def _capture_query(store: list):
    async def fake_query(prompt, options):
        store.append(prompt)
        if False:
            yield None
    return fake_query


def test_follow_up_prompt_has_no_name_or_email(tmp_path):
    import advance_sequences
    import sequencing

    deployment = _FakeDeployment(tmp_path)
    vault = ContactVault(deployment)
    vault.remember(_person())
    deployment.store.write_text(deployment.state_key("drafts_audit.json"), json.dumps([
        {"tracking_id": "tr-1", "before_text": f"Hi {FIRST}, an older draft stored with the real name."}]))
    sequencing.seed_step_zero(deployment, [{"to": EMAIL, "tracking_id": "tr-1", "thread_id": "th-1",
                                            "account_name": "Acme", "stakeholder_id": "p1",
                                            "stakeholder_title": "VP Sales", "subject": f"For {FIRST}"}])
    entry = sequencing.load_sequences(deployment)[0]
    prompts: list = []
    with patch.object(advance_sequences, "query", _capture_query(prompts)), \
         patch.object(advance_sequences, "MailboxClient", MagicMock()):
        asyncio.run(advance_sequences._draft_next_step(deployment, entry, 1, None))
    assert prompts and "VP Sales" in prompts[0]
    assert_no_pii(prompts[0])


def test_learning_prompt_is_depersonalized(tmp_path):
    import poll_for_edits

    deployment = _FakeDeployment(tmp_path)
    vault = ContactVault(deployment)
    vault.remember(_person())
    deployment.store.write_text(deployment.state_key("drafts_audit.json"), json.dumps([{
        "tracking_id": "tr-1", "draft_ref": "d1", "status": "open", "to": EMAIL, "stakeholder_id": "p1",
        "stakeholder_name": NAME, "stakeholder_title": "VP Sales", "account_name": "Acme",
        "before_text": "Hi {{first_name}},\nWe help teams.\nThanks"}]))
    mailbox = MagicMock()
    mailbox.get_finalized_text.return_value = f"Hi {FIRST},\nWe help revenue teams, {EMAIL}.\nBest, Frederik"
    prompts: list = []
    with patch.object(poll_for_edits, "MailboxClient", return_value=mailbox), \
         patch.object(poll_for_edits, "query", _capture_query(prompts)), \
         patch.object(poll_for_edits.sequencing, "mark_sent", lambda *a: None):
        asyncio.run(poll_for_edits.poll_once(deployment, dry_run=False))
    assert prompts and "{{first_name}}" in prompts[0] and "{{sender_name}}" in prompts[0]
    assert_no_pii(prompts[0])
    assert_no_pii(deployment.store.read_text(deployment.state_key("learning", "edit_diffs_log.jsonl")))


# ---- Client folders ---------------------------------------------------------------------------

def test_deployment_loads_from_client_folder_and_rejects_double_claims(tmp_path, monkeypatch):
    import deployment as dep

    def make(client, did):
        d = tmp_path / client / "outbound"
        d.mkdir(parents=True)
        (d / "outbound.yaml").write_text(f"deployment_id: {did}\nmail_provider: gmail\n")
        (d / "icp.md").write_text("icp")
        (d / "positioning.md").write_text("pos")
        (d / "sequence.yaml").write_text("steps:\n  - {name: opener, wait_days: 4, angle: a}\n")

    make("Acme", "acme")
    monkeypatch.setattr(dep, "CLIENTS_DIR", tmp_path)
    loaded = dep.load_deployment("acme")
    assert loaded.client == "Acme" and loaded.read_icp_doc() == "icp" and dep.list_deployments() == ["acme"]

    make("Other", "acme")
    with pytest.raises(ValueError):
        dep.load_deployment("acme")
