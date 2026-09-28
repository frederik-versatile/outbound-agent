import json
from unittest.mock import patch

import dashboard
from dashboard import app, compute_dashboard_data
from store import LocalFileStore


class _FakeDeployment:
    def __init__(self, tmp_path):
        self.deployment_id = "test"
        self.store = LocalFileStore(tmp_path)

    def state_key(self, *parts):
        return f"state/{self.deployment_id}/" + "/".join(parts)


def _write_audit(deployment, entries):
    deployment.store.write_text(deployment.state_key("drafts_audit.json"), json.dumps(entries))


def _write_sequences(deployment, entries):
    deployment.store.write_text(deployment.state_key("sequences.json"), json.dumps(entries))


def test_empty_deployment_has_zero_stats(tmp_path):
    d = _FakeDeployment(tmp_path)
    data = compute_dashboard_data(d)
    assert data == {"sends": 0, "replies": 0, "reply_rate": 0.0, "recipients": []}


def test_only_sent_statuses_count_as_sends(tmp_path):
    d = _FakeDeployment(tmp_path)
    _write_audit(d, [
        {"to": "a@x.com", "status": "open", "stakeholder_name": "A", "account_name": "X", "subject": "s1"},
        {"to": "b@x.com", "status": "unchanged_sent", "stakeholder_name": "B", "account_name": "X", "subject": "s2", "sent_at": "2026-01-01T00:00:00+00:00"},
        {"to": "c@x.com", "status": "learned_sent", "stakeholder_name": "C", "account_name": "X", "subject": "s3", "sent_at": "2026-01-02T00:00:00+00:00"},
        {"to": "d@x.com", "status": "unchanged_stable_unsent", "stakeholder_name": "D", "account_name": "X", "subject": "s4"},
    ])
    data = compute_dashboard_data(d)
    assert data["sends"] == 2
    assert {r["to"] for r in data["recipients"]} == {"b@x.com", "c@x.com"}


def test_reply_rate_computed_from_sequence_status(tmp_path):
    d = _FakeDeployment(tmp_path)
    _write_audit(d, [
        {"to": "b@x.com", "status": "unchanged_sent", "stakeholder_name": "B", "account_name": "X", "subject": "s2", "sent_at": "2026-01-01T00:00:00+00:00"},
        {"to": "c@x.com", "status": "learned_sent", "stakeholder_name": "C", "account_name": "X", "subject": "s3", "sent_at": "2026-01-02T00:00:00+00:00"},
    ])
    _write_sequences(d, [
        {"lead_key": "b@x.com", "status": "replied"},
        {"lead_key": "c@x.com", "status": "awaiting_reply"},
    ])
    data = compute_dashboard_data(d)
    assert data["sends"] == 2
    assert data["replies"] == 1
    assert data["reply_rate"] == 50.0
    replied_row = next(r for r in data["recipients"] if r["to"] == "b@x.com")
    assert replied_row["replied"] is True
    not_replied_row = next(r for r in data["recipients"] if r["to"] == "c@x.com")
    assert not_replied_row["replied"] is False


def test_recipients_are_case_insensitively_matched_and_sorted_newest_first(tmp_path):
    d = _FakeDeployment(tmp_path)
    _write_audit(d, [
        {"to": "OLD@x.com", "status": "unchanged_sent", "stakeholder_name": "Old", "account_name": "X", "subject": "s1", "sent_at": "2026-01-01T00:00:00+00:00"},
        {"to": "new@x.com", "status": "unchanged_sent", "stakeholder_name": "New", "account_name": "X", "subject": "s2", "sent_at": "2026-01-05T00:00:00+00:00"},
    ])
    _write_sequences(d, [{"lead_key": "old@x.com", "status": "replied"}])
    data = compute_dashboard_data(d)
    assert data["recipients"][0]["to"] == "new@x.com"  # newest first
    assert data["recipients"][1]["replied"] is True  # matched despite case difference


def test_auth_required_without_credentials():
    client = app.test_client()
    resp = client.get("/")
    assert resp.status_code == 401


def test_auth_fails_closed_when_env_vars_missing(monkeypatch):
    monkeypatch.delenv("DASHBOARD_USERNAME", raising=False)
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    client = app.test_client()
    resp = client.get("/", headers={"Authorization": "Basic YWRtaW46YWRtaW4="})  # admin:admin
    assert resp.status_code == 401


def test_correct_credentials_pass_auth(monkeypatch, tmp_path):
    monkeypatch.setenv("DASHBOARD_USERNAME", "admin")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "secret123")
    monkeypatch.setenv("DEPLOYMENT_ID", "test")

    fake_deployment = _FakeDeployment(tmp_path)
    client = app.test_client()
    with patch.object(dashboard, "load_deployment", return_value=fake_deployment):
        resp = client.get("/", auth=("admin", "secret123"))

    assert resp.status_code == 200
    assert b"Outbound Agent" in resp.data


def test_wrong_credentials_rejected(monkeypatch):
    monkeypatch.setenv("DASHBOARD_USERNAME", "admin")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "secret123")
    client = app.test_client()
    resp = client.get("/", auth=("admin", "wrong"))
    assert resp.status_code == 401
